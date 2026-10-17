window.addEventListener("DOMContentLoaded", () => {
  console.log("[RyouU] Renderer initialized");

  // ── DOM Elements ──────────────────────────────────────────────────────────
  const statusDot            = document.getElementById("statusDot");
  const statusText           = document.getElementById("statusText");
  const flagBadge            = document.getElementById("flagBadge");
  const flagCount            = document.getElementById("flagCount");
  const flagAlertBanner      = document.getElementById("flagAlertBanner");
  const flagAlertTitle       = document.getElementById("flagAlertTitle");
  const flagAlertDetails     = document.getElementById("flagAlertDetails");
  const dismissFlagBtn       = document.getElementById("dismissFlagBtn");
  const verifiedPill         = document.getElementById("verifiedPill");
  const verifiedText         = document.getElementById("verifiedText");

  // Modality status pills
  const videoBadge           = document.getElementById("videoBadge");
  const audioBadge           = document.getElementById("audioBadge");
  const speakerBadge         = document.getElementById("speakerBadge");

  // Candidate + gateway
  const candidateIdInput     = document.getElementById("candidateIdInput");
  const gatewayStatusBadge   = document.getElementById("gatewayStatusBadge");

  // Buttons & controls
  const selectButton         = document.getElementById("selectButton");
  const autoMonitorButton    = document.getElementById("autoMonitorButton");
  const recordOnceButton     = document.getElementById("recordOnceButton");
  const testAudioBtn         = document.getElementById("testAudioBtn");
  const closeButton          = document.getElementById("closeButton");
  const intervalSelect       = document.getElementById("intervalSelect");
  const simulateSelect       = document.getElementById("simulateSelect");
  const countdownDisplay     = document.getElementById("countdownDisplay");
  const regionValue          = document.getElementById("regionValue");
  const outputText           = document.getElementById("outputText");

  // Consent modal
  const consentModal         = document.getElementById("consentModal");
  const consentConfirmBtn    = document.getElementById("consentConfirmBtn");
  const consentCancelBtn     = document.getElementById("consentCancelBtn");

  // ── State ─────────────────────────────────────────────────────────────────
  const State = { IDLE: "IDLE", WAITING: "WAITING", RECORDING: "RECORDING", ANALYZING: "ANALYZING", STOPPING: "STOPPING" };

  let currentState       = State.IDLE;
  let isAutoMonitoring   = false;
  let hasUserConsented   = false;
  let selectedRegion     = null;
  let flaggedCount       = 0;
  let currentGeneration  = 0;

  // Active capture resources
  let activeStream       = null;   // desktop video stream
  let activeMicStream    = null;   // system audio stream
  let activeDisplayStream= null;   // full getDisplayMedia stream (kept alive so loopback audio keeps flowing)
  let audioMeterCtx      = null;   // level meter for system audio
  let audioMeterTimer    = null;
  let lastSystemAudioPeak= 0;      // peak level (0..1) seen during the last recording
  let activeRecorder     = null;   // video MediaRecorder
  let activeAudioRecorder= null;   // audio MediaRecorder
  let animationFrame     = null;
  let countdownTimer     = null;
  let recordingTimer     = null;
  let lastVideoResult    = null;   // last video verification result
  let lastAudioResult    = null;   // last audio verification result

  // ── Helpers ───────────────────────────────────────────────────────────────
  function updateIntegrityVerifiedState() {
    const isVideoVerified = Boolean(lastVideoResult && lastVideoResult.success && lastVideoResult.is_real);
    const isAudioCloudVerified = Boolean(lastAudioResult && lastAudioResult.cloud_verified === true);

    // Requirement 2: Never report overall success or "Integrity Verified" unless BOTH required cloud checks completed with valid responses.
    if (isVideoVerified && isAudioCloudVerified && flaggedCount === 0) {
      showVerified("Integrity Verified — CLIP ViT-L/14 & Detectif.ai Cloud");
      setStatus("All checks verified", "#22c55e");
    } else {
      if (verifiedPill) {
        verifiedPill.classList.add("hidden");
      }
    }
  }
  function getCandidateId() {
    return (candidateIdInput && candidateIdInput.value.trim()) || "candidate-001";
  }

  function getSimulateAnomaly() {
    return (simulateSelect && simulateSelect.value !== "none") ? simulateSelect.value : null;
  }

  function setStatus(message, color = "#22c55e", pulsing = false) {
    if (statusText) statusText.textContent = message;
    if (statusDot) {
      statusDot.style.backgroundColor = color;
      statusDot.classList.toggle("pulsing", pulsing);
    }
    console.log(`[RyouU] STATUS: ${message}`);
  }

  function showOutput(message) {
    if (outputText) outputText.textContent = message;
    console.log(`[RyouU] OUTPUT: ${message.replace(/\n/g, " | ")}`);
  }

  function setInteractiveDisabled(disabled) {
    if (selectButton)      selectButton.disabled      = disabled;
    if (intervalSelect)    intervalSelect.disabled    = disabled;
    if (simulateSelect)    simulateSelect.disabled    = disabled;
    if (recordOnceButton)  recordOnceButton.disabled  = disabled;
    if (testAudioBtn)      testAudioBtn.disabled      = disabled;
    if (candidateIdInput)  candidateIdInput.disabled  = disabled;
  }

  function setModalityPill(pill, state, label) {
    if (!pill) return;
    pill.className = `modality-pill status-${state}`;
    pill.textContent = label;
  }

  function raiseFlag(title, details) {
    flaggedCount += 1;
    if (flagCount)    flagCount.textContent = String(flaggedCount);
    if (flagBadge)    flagBadge.classList.add("has-flags");
    if (verifiedPill) verifiedPill.classList.add("hidden");

    if (flagAlertBanner) {
      flagAlertBanner.classList.remove("hidden");
      if (flagAlertTitle)   flagAlertTitle.textContent   = title;
      if (flagAlertDetails) flagAlertDetails.textContent = details;
    }
  }

  function showVerified(label) {
    if (flagAlertBanner) flagAlertBanner.classList.add("hidden");
    if (verifiedPill) {
      verifiedPill.classList.remove("hidden");
      if (verifiedText) verifiedText.textContent = label;
    }
  }

  // ── Guard ─────────────────────────────────────────────────────────────────
  if (!window.ryouu) {
    setStatus("Preload error", "#ef4444");
    showOutput("Electron preload API is unavailable.");
    console.error("[RyouU] window.ryouu is missing");
    return;
  }

  // ── Startup: probe Supabase gateway ───────────────────────────────────────
  (async () => {
    try {
      const res = await window.ryouu.testSupabaseConnection();
      if (res && res.success && res.configured) {
        if (gatewayStatusBadge) gatewayStatusBadge.textContent = "⚡ Gateway: Supabase ✓";
        console.log("[RyouU] Supabase gateway connected.");
      } else if (res && res.configured === false) {
        if (gatewayStatusBadge) gatewayStatusBadge.textContent = "⚡ Gateway: Local Debug";
        console.log("[RyouU] Supabase not configured — local debug mode.");
      } else {
        if (gatewayStatusBadge) gatewayStatusBadge.textContent = "⚡ Gateway: Error";
        console.warn("[RyouU] Supabase gateway error:", res?.message);
      }
    } catch (e) {
      console.warn("[RyouU] Gateway probe failed:", e);
    }
  })();

  // ── Event: Supabase gateway badge click → list available audio ────────────
  if (gatewayStatusBadge) {
    gatewayStatusBadge.addEventListener("click", async () => {
      if (gatewayStatusBadge) gatewayStatusBadge.textContent = "⚡ Listing…";
      try {
        const res = await window.ryouu.listReferenceAudio();
        if (res && res.success) {
          const localCount    = res.local_files?.length   || 0;
          const supabaseCount = res.supabase_files?.length || 0;
          const source = res.supabase_configured ? "Supabase ✓" : "Local Debug";
          gatewayStatusBadge.textContent = `⚡ ${source} (${localCount}L ${supabaseCount}S files)`;
          const localNames = (res.local_files || []).map(f => f.name).join(", ") || "none";
          showOutput(
            `Reference Audio Inventory\n` +
            `  Local debug files: ${localNames}\n` +
            `  Supabase bucket files: ${supabaseCount}\n` +
            `  Source mode: ${source}`
          );
        } else {
          gatewayStatusBadge.textContent = "⚡ Gateway: Error";
          showOutput(`Gateway list error: ${res?.error || "unknown"}`);
        }
      } catch (e) {
        gatewayStatusBadge.textContent = "⚡ Gateway: Error";
        showOutput(`Gateway error: ${e.message}`);
      }
    });
  }

  // ── Event: Zoom detection ─────────────────────────────────────────────────
  window.ryouu.onZoomStatus((detected) => {
    if (currentState === State.IDLE) {
      setStatus(
        detected ? "Zoom detected" : "Waiting for interview app",
        detected ? "#22c55e" : "#a78bfa"
      );
    }
  });

  // ── Event: Region selected ────────────────────────────────────────────────
  window.ryouu.onRegionSelected((region) => {
    selectedRegion = region;
    if (regionValue) {
      regionValue.textContent = `${Math.round(region.width)}×${Math.round(region.height)} px`;
    }
    setStatus("Region selected", "#22c55e");
    showOutput(
      `Candidate tile selected (${Math.round(region.width)}×${Math.round(region.height)}).\n` +
      "Ready to start 1-minute auto-monitoring or record a 25s clip."
    );
  });

  // ── Event: Video deepfake flag (from main via push) ───────────────────────
  window.ryouu.onDeepfakeFlag((result) => {
    handleVideoVerificationResult(result);
  });

  // ── Video verification result handler ─────────────────────────────────────
  function handleVideoVerificationResult(analysis) {
    console.log("[RyouU] Video analysis:", analysis);
    lastVideoResult = analysis;

    // Skip if model fails or was not able to verify — zero fallbacks, no false alarms
    if (!analysis || !analysis.success || analysis.skipped) {
      const errorMsg = analysis?.error || "CLIP ViT-L/14 model execution failed or skipped.";
      console.warn("[RyouU] Video verification skipped due to model error:", errorMsg);
      setModalityPill(videoBadge, "skipped", "📹 Video: Skipped");
      showOutput(`[CLIP ViT-L/14] Skipped (Model error: ${errorMsg})`);
      updateIntegrityVerifiedState();
      return;
    }

    const realPct = Math.round(((analysis.real_confidence !== undefined) ? analysis.real_confidence : (1.0 - (analysis.anomaly_score || 0))) * 100);
    const scorePct = Math.round((analysis.anomaly_score || 0) * 100);
    const isAnomaly = Boolean(analysis.is_anomaly);

    if (isAnomaly) {
      setModalityPill(videoBadge, "flagged", `📹 Video: Flagged (Real: ${realPct}% < 70%)`);
      const reasons = analysis.flag_reasons?.join(" • ") || `Video real confidence (${realPct}%) is below the required 70% threshold.`;
      raiseFlag(
        `VIDEO INTEGRITY ALERT (Real: ${realPct}%, Fake: ${scorePct}%)`,
        `${reasons}\nModel: ${analysis.model || "CLIP ViT-L/14"}`
      );
      setStatus(`Video flagged (${realPct}% real)`, "#ef4444");
    } else {
      setModalityPill(videoBadge, "ok", `📹 Video: Real ${realPct}% (≥70%)`);
    }

    updateIntegrityVerifiedState();
  }

  // ── Audio verification result handler (Single delivery, de-duplicated) ─────
  let lastProcessedAudioToken = null;

  function handleAudioVerificationResult(analysis) {
    if (!analysis) {
      setModalityPill(audioBadge, "error", "🎙️ Voice: No Data");
      setModalityPill(speakerBadge, "error", "👤 Speaker: No Data");
      showOutput("Audio verify error: No response returned.");
      updateIntegrityVerifiedState();
      return;
    }

    // Single result delivery & de-duplication guard: ensure each verification updates UI exactly once
    const resultToken = analysis.request_id ||
      analysis.speaker_verification?.request_id ||
      analysis.audio_deepfake?.request_id ||
      (analysis.speaker_verification?.similarity != null ? `${analysis.candidate_id}-${analysis.speaker_verification.decision}-${analysis.speaker_verification.similarity}` : null) ||
      (analysis.error ? `err-${analysis.error_code}-${analysis.error}` : null);

    if (resultToken && resultToken === lastProcessedAudioToken) {
      console.log("[RyouU] Skipping duplicate audio verification handling for token:", resultToken);
      return;
    }
    if (resultToken) {
      lastProcessedAudioToken = resultToken;
    }

    console.log("[RyouU] Audio analysis (processing once):", analysis);
    lastAudioResult = analysis;

    const deepfake = analysis.audio_deepfake     || {};
    const speaker  = analysis.speaker_verification || {};
    const hasAnomaly = Boolean(analysis.has_audio_anomaly);
    const hasInconclusive = Boolean(analysis.has_inconclusive);
    const hasError = Boolean(analysis.has_error || !analysis.success);

    // ── 1. Aletheia Badge (Voice Deepfake Detection) ─────────────────────────
    if (deepfake.is_cloud === false) {
      if (deepfake.status === "error") {
        setModalityPill(audioBadge, "error", `🎙️ Voice: ${deepfake.error_code || "Err"}`);
      } else {
        setModalityPill(audioBadge, "debug", `🎙️ Voice: [DEBUG] ${deepfake.verdict || "Local"}`);
      }
    } else if (deepfake.status === "error") {
      setModalityPill(audioBadge, "error", `🎙️ Voice: Err (${deepfake.error_code || "API"})`);
    } else if (deepfake.verdict === "fake") {
      const scoreStr = deepfake.score != null ? deepfake.score.toFixed(2) : "High";
      setModalityPill(audioBadge, "flagged", `🎙️ Voice: FAKE (${scoreStr})`);
    } else if (deepfake.verdict === "not_sure") {
      setModalityPill(audioBadge, "inconclusive", "🎙️ Voice: Inconclusive");
    } else if (deepfake.verdict === "real") {
      setModalityPill(audioBadge, "ok", "🎙️ Voice: Real (Cloud)");
    } else {
      setModalityPill(audioBadge, "inconclusive", `🎙️ Voice: ${deepfake.verdict || "Indeterminate"}`);
    }

    // ── 2. Eidos Badge (Speaker Verification) ────────────────────────────────
    // NOTE: Similarity is between -1 and 1, NOT a percentage. Never label as percentage.
    if (speaker.is_cloud === false) {
      if (speaker.status === "error") {
        setModalityPill(speakerBadge, "error", `👤 Speaker: ${speaker.error_code || "Err"}`);
      } else {
        setModalityPill(speakerBadge, "debug", `👤 Speaker: [DEBUG] ${speaker.decision || "Local"}`);
      }
    } else if (speaker.status === "error") {
      setModalityPill(speakerBadge, "error", `👤 Speaker: Err (${speaker.error_code || "API"})`);
    } else if (speaker.decision === "no_match") {
      const simStr = speaker.similarity_formatted || (speaker.similarity != null ? speaker.similarity.toFixed(2) : "Low");
      setModalityPill(speakerBadge, "flagged", `👤 Speaker: MISMATCH [sim: ${simStr}]`);
    } else if (speaker.decision === "match") {
      const simStr = speaker.similarity_formatted || (speaker.similarity != null ? speaker.similarity.toFixed(2) : "Match");
      setModalityPill(speakerBadge, "ok", `👤 Speaker: Match [sim: ${simStr}]`);
    } else {
      setModalityPill(speakerBadge, "inconclusive", "👤 Speaker: Indeterminate");
    }

    // ── 3. Alerts & Output Reporting (Executed exactly once) ────────────────
    const spkDetails = [];
    if (speaker.decision) spkDetails.push(`Decision: ${speaker.decision.toUpperCase()}`);
    if (speaker.similarity != null) spkDetails.push(`Similarity: ${speaker.similarity_formatted || (speaker.similarity >= 0 ? "+" : "") + speaker.similarity}`);
    if (speaker.threshold != null) spkDetails.push(`Threshold: ${speaker.threshold}`);
    if (speaker.request_id) spkDetails.push(`Req: ${speaker.request_id}`);
    if (speaker.operation_id) spkDetails.push(`Op: ${speaker.operation_id}`);

    const spkLine = spkDetails.length > 0 ? `• Speaker (Eidos): ${spkDetails.join(" | ")}` : "";
    const dfLine = deepfake.verdict ? `• Deepfake (Aletheia): ${deepfake.verdict.toUpperCase()}${deepfake.score != null ? ` (score: ${deepfake.score.toFixed(4)})` : ""}${deepfake.request_id ? ` [Req: ${deepfake.request_id}]` : ""}` : "";
    const diagBlock = [spkLine, dfLine].filter(Boolean).join("\n");

    if (hasAnomaly) {
      const reasons = (analysis.flag_reasons || []).join("\n• ");
      raiseFlag(
        "AUDIO ANOMALY FLAGGED",
        `• ${reasons}\n\nCandidate: ${analysis.candidate_id || "N/A"}\nModels: ${deepfake.model || "Aletheia"} | ${speaker.model || "Eidos"}`
      );
      setStatus("Audio anomaly flagged", "#ef4444");
      if (diagBlock) showOutput(`[Audio Anomaly Flagged]\n${diagBlock}`);
    } else if (hasInconclusive) {
      setStatus("Audio check inconclusive (not_sure)", "#f59e0b");
      showOutput(`[Detectif.ai Inconclusive]\nAletheia returned 'not_sure' (low clarity or insufficient length).\n${diagBlock}`);
    } else if (hasError) {
      const reasons = (analysis.flag_reasons || []).join(" | ") || analysis.error || "Audio check failed.";
      setStatus("Audio verification error", "#f59e0b");
      showOutput(`[Detectif.ai Error] ${reasons}`);
    } else if (analysis.cloud_verified) {
      setStatus("Audio verified by Detectif.ai", "#22c55e");
      if (diagBlock) showOutput(`[Detectif.ai Verified]\n${diagBlock}`);
    } else {
      setStatus("Local debug checks completed", "#93c5fd");
      if (diagBlock) showOutput(`[Local Debug]\n${diagBlock}`);
    }

    updateIntegrityVerifiedState();
  }

  // ── UI: dismiss flag banner ───────────────────────────────────────────────
  if (dismissFlagBtn) {
    dismissFlagBtn.addEventListener("click", () => {
      if (flagAlertBanner) flagAlertBanner.classList.add("hidden");
    });
  }

  // ── UI: Select Candidate Region ───────────────────────────────────────────
  if (selectButton) {
    selectButton.addEventListener("click", async () => {
      if (currentState === State.RECORDING) {
        showOutput("Wait for current recording to finalize before selecting region.");
        return;
      }
      try {
        setStatus("Selecting region…", "#f59e0b");
        showOutput("Drag a rectangle around the candidate's video tile.");
        const result = await window.ryouu.openCapturePicker();
        if (result && result.success === false) throw new Error(result.error || "Could not open picker.");
      } catch (error) {
        console.error("[RyouU] Region picker error:", error);
        setStatus("Selection error", "#ef4444");
        showOutput(error.message || String(error));
      }
    });
  }

  // ── Interval helper ───────────────────────────────────────────────────────
  function getSelectedIntervalSeconds() {
    const val = intervalSelect ? intervalSelect.value : "60";
    if (val === "random") return Math.floor(Math.random() * (300 - 120 + 1)) + 120;
    const parsed = parseInt(val, 10);
    return isNaN(parsed) || parsed < 5 ? 60 : parsed;
  }

  // ── Audio capture helpers ─────────────────────────────────────────────────
  async function startMicCapture() {
    try {
      // System-level (loopback) audio only — the audio received from the meeting, not the microphone.
      const displayStream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
      // Keep the video track alive (not recorded): stopping it can end the loopback audio capture.
      activeDisplayStream = displayStream;
      const audioTracks = displayStream.getAudioTracks();
      if (audioTracks.length === 0) {
        console.warn("[RyouU] System audio loopback returned no audio track.");
        displayStream.getTracks().forEach(t => t.stop());
        activeDisplayStream = null;
        return null;
      }
      activeMicStream = new MediaStream(audioTracks);

      // Level meter so a silent capture is detected instead of being sent to the API.
      lastSystemAudioPeak = 0;
      try {
        audioMeterCtx = new (window.AudioContext || window.webkitAudioContext)();
        if (audioMeterCtx.state === "suspended") await audioMeterCtx.resume();
        const analyser = audioMeterCtx.createAnalyser();
        analyser.fftSize = 2048;
        audioMeterCtx.createMediaStreamSource(activeMicStream).connect(analyser);
        const buf = new Float32Array(analyser.fftSize);
        audioMeterTimer = setInterval(() => {
          analyser.getFloatTimeDomainData(buf);
          for (let i = 0; i < buf.length; i++) {
            const v = Math.abs(buf[i]);
            if (v > lastSystemAudioPeak) lastSystemAudioPeak = v;
          }
        }, 100);
      } catch (meterErr) {
        console.warn("[RyouU] Audio level meter unavailable:", meterErr.message);
      }
      return activeMicStream;
    } catch (e) {
      console.warn("[RyouU] System audio not available:", e.message);
      return null;
    }
  }

  function stopMicCapture() {
    if (audioMeterTimer) { clearInterval(audioMeterTimer); audioMeterTimer = null; }
    if (audioMeterCtx) { try { audioMeterCtx.close(); } catch (e) { /* ignore */ } audioMeterCtx = null; }
    if (activeMicStream) {
      activeMicStream.getTracks().forEach(t => t.stop());
      activeMicStream = null;
    }
    if (activeDisplayStream) {
      activeDisplayStream.getTracks().forEach(t => t.stop());
      activeDisplayStream = null;
    }
    activeAudioRecorder = null;
  }

  /**
   * Records system (loopback) audio for durationSeconds, encodes as WAV (PCM),
   * saves it via IPC, then calls Detectif.ai verification.
   */
  async function recordAndVerifyAudio(durationSeconds, generation) {
    const candidateId = getCandidateId();
    const simulateAnomaly = getSimulateAnomaly();

    const micStream = await startMicCapture();
    if (!micStream) {
      setModalityPill(audioBadge,   "skipped", "🎙️ Voice: No System Audio");
      setModalityPill(speakerBadge, "skipped", "👤 Speaker: No System Audio");
      showOutput(prev => prev); // leave existing output
      return null;
    }

    const audioChunks = [];
    const supportedAudioMime = (() => {
      const types = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/ogg"];
      return types.find(t => MediaRecorder.isTypeSupported(t)) || "";
    })();

    activeAudioRecorder = new MediaRecorder(micStream, supportedAudioMime ? { mimeType: supportedAudioMime } : {});

    activeAudioRecorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) audioChunks.push(e.data);
    };

    activeAudioRecorder.start(250);

    // Wait for the same duration as video
    await new Promise((resolve) => {
      setTimeout(() => {
        if (activeAudioRecorder && activeAudioRecorder.state !== "inactive") {
          activeAudioRecorder.onstop = resolve;
          activeAudioRecorder.stop();
        } else {
          resolve();
        }
      }, durationSeconds * 1000);
    });

    stopMicCapture();

    if (generation !== currentGeneration) return null;
    if (audioChunks.length === 0) return null;

    // Preserve actual MIME type and container format
    const actualMime = (activeAudioRecorder && activeAudioRecorder.mimeType) || supportedAudioMime || "audio/webm";
    const audioBlob = new Blob(audioChunks, { type: actualMime });
    const audioBytes = new Uint8Array(await audioBlob.arrayBuffer());
    const saveRes = await window.ryouu.saveAudioRecording({ bytes: audioBytes, mimeType: actualMime });

    if (!saveRes || !saveRes.success) {
      console.warn("[RyouU] Audio save failed:", saveRes?.error);
      setModalityPill(audioBadge,   "error", "🎙️ Voice: Save Err");
      setModalityPill(speakerBadge, "error", "👤 Speaker: Save Err");
      return null;
    }

    console.log("[RyouU] Audio saved to:", saveRes.filePath, "MIME:", saveRes.mimeType);

    // Kick off Detectif.ai verification (async, non-blocking)
    setModalityPill(audioBadge,   "evaluating", "🎙️ Voice: Analyzing…");
    setModalityPill(speakerBadge, "evaluating", "👤 Speaker: Verifying…");

    window.ryouu.verifyAudio({
      audioPath: saveRes.filePath,
      candidateId,
      mimeType: saveRes.mimeType || actualMime,
      simulateAnomaly: simulateAnomaly || undefined
    }).then((res) => {
      handleAudioVerificationResult(res);
    }).catch((err) => {
      console.error("[RyouU] Audio verification error:", err);
      setModalityPill(audioBadge,   "error", "🎙️ Voice: Err");
      setModalityPill(speakerBadge, "error", "👤 Speaker: Err");
    });

    return saveRes;
  }

  // ── Core: record one 25-second clip + audio ────────────────────────────────
  async function recordClip(durationSeconds = 25, generation) {
    if (!selectedRegion) throw new Error("No candidate region selected.");

    currentState = State.RECORDING;
    setStatus(`Recording: ${durationSeconds}s`, "#ef4444", true);

    // Reset modality pills to evaluating
    setModalityPill(videoBadge,   "evaluating", "📹 Video: Recording…");
    setModalityPill(audioBadge,   "evaluating", "🎙️ Voice: Recording…");
    setModalityPill(speakerBadge, "evaluating", "👤 Speaker: Pending…");

    let video       = null;
    let canvas      = null;
    let canvasStream = null;
    let chunks      = [];
    let remaining   = durationSeconds;

    if (countdownDisplay) countdownDisplay.textContent = `Rec: ${remaining}s`;

    const intervalCountTimer = setInterval(() => {
      remaining -= 1;
      if (remaining >= 0 && countdownDisplay && currentState === State.RECORDING) {
        countdownDisplay.textContent = `Rec: ${remaining}s`;
      }
    }, 1000);

    // Start audio capture in parallel (non-blocking)
    const audioPromise = recordAndVerifyAudio(durationSeconds, generation);

    try {
      const sourceResult = await window.ryouu.getRecordingSource();
      if (!sourceResult || !sourceResult.success) {
        throw new Error(sourceResult?.error || "Could not access screen source.");
      }

      const { sourceId, region } = sourceResult;

      activeStream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: {
          mandatory: {
            chromeMediaSource: "desktop",
            chromeMediaSourceId: sourceId
          }
        }
      });

      if (generation !== currentGeneration) throw new Error("Session stopped before capture.");

      video = document.createElement("video");
      video.muted = true;
      video.playsInline = true;
      video.srcObject = activeStream;

      await new Promise((resolve, reject) => {
        video.onloadedmetadata = resolve;
        video.onerror = () => reject(new Error("Failed to load screen stream."));
      });

      await video.play();

      if (!video.videoWidth || !video.videoHeight) throw new Error("Screen source returned 0 dimensions.");

      const scaleX = video.videoWidth  / region.displayWidth;
      const scaleY = video.videoHeight / region.displayHeight;

      const cropX = Math.max(0, Math.round(region.x * scaleX));
      const cropY = Math.max(0, Math.round(region.y * scaleY));
      const cropWidth  = Math.min(Math.round(region.width  * scaleX), video.videoWidth  - cropX);
      const cropHeight = Math.min(Math.round(region.height * scaleY), video.videoHeight - cropY);

      if (cropWidth < 2 || cropHeight < 2) throw new Error("Crop coordinates out of bounds.");

      canvas = document.createElement("canvas");
      canvas.width  = cropWidth;
      canvas.height = cropHeight;
      const ctx = canvas.getContext("2d");

      function renderFrame() {
        if (currentState !== State.RECORDING) return;
        ctx.drawImage(video, cropX, cropY, cropWidth, cropHeight, 0, 0, canvas.width, canvas.height);
        animationFrame = requestAnimationFrame(renderFrame);
      }
      renderFrame();

      canvasStream = canvas.captureStream(20);
      const mimeType = MediaRecorder.isTypeSupported("video/webm;codecs=vp8")
        ? "video/webm;codecs=vp8"
        : "video/webm";

      activeRecorder = new MediaRecorder(canvasStream, { mimeType });
      chunks = [];
      activeRecorder.ondataavailable = (e) => { if (e.data && e.data.size > 0) chunks.push(e.data); };
      activeRecorder.start(250);

      // Wait for clip to finish
      await new Promise((resolve, reject) => {
        recordingTimer = setTimeout(() => {
          if (activeRecorder && activeRecorder.state !== "inactive") {
            activeRecorder.onstop = resolve;
            activeRecorder.stop();
          } else {
            resolve();
          }
        }, durationSeconds * 1000);
        activeRecorder.onerror = (err) => reject(err);
      });

      clearInterval(intervalCountTimer);

      if (generation !== currentGeneration) {
        console.log("[RyouU] Session stopped during recording.");
        return null;
      }

      const blob = new Blob(chunks, { type: "video/webm" });
      if (blob.size === 0) throw new Error("Recorded video is empty.");

      currentState = State.ANALYZING;
      setStatus("Analyzing clip…", "#a855f7");
      if (countdownDisplay) countdownDisplay.textContent = "Analyzing…";

      showOutput(
        `Recorded 25s clip (${(blob.size / (1024 * 1024)).toFixed(2)} MB).\n` +
        `Running CLIP ViT-L/14 (video) and Detectif.ai (audio/speaker)…`
      );

      const bytes = new Uint8Array(await blob.arrayBuffer());
      const saveResult = await window.ryouu.saveRecording(bytes);
      if (!saveResult || !saveResult.success) throw new Error(saveResult?.error || "Failed to save video.");

      console.log("[RyouU] Video saved:", saveResult.filePath);

      // Run video deepfake check
      setModalityPill(videoBadge, "evaluating", "📹 Video: Analyzing…");
      window.ryouu.verifyDeepfake(saveResult.filePath).then((analysis) => {
        handleVideoVerificationResult(analysis);
      }).catch((err) => {
        console.error("[RyouU] Video verification error:", err);
        handleVideoVerificationResult({
          success: false,
          skipped: true,
          error: err.message || String(err)
        });
      });

      // Wait for audio job to finish (it's already running in parallel)
      await audioPromise;

      return saveResult;
    } finally {
      clearInterval(intervalCountTimer);
      if (recordingTimer) { clearTimeout(recordingTimer); recordingTimer = null; }
      if (animationFrame !== null) { cancelAnimationFrame(animationFrame); animationFrame = null; }
      if (canvasStream) canvasStream.getTracks().forEach(t => t.stop());
      if (activeStream) { activeStream.getTracks().forEach(t => t.stop()); activeStream = null; }
      if (video) { video.pause(); video.srcObject = null; }
      activeRecorder = null;
    }
  }

  // ── Scheduler ─────────────────────────────────────────────────────────────
  function formatTime(s) {
    const mins = Math.floor(s / 60);
    const secs = s % 60;
    return `${mins}:${secs < 10 ? "0" : ""}${secs}`;
  }

  function scheduleNextCycle() {
    if (!isAutoMonitoring) return;

    const generation  = currentGeneration;
    const intervalSec = getSelectedIntervalSeconds();
    let remainingSec  = intervalSec;

    currentState = State.WAITING;
    setStatus("Auto Monitor Active", "#22c55e");
    if (countdownDisplay) countdownDisplay.textContent = `Next: ${formatTime(remainingSec)}`;
    showOutput(
      `Waiting ${intervalSec}s before next 25s clip.\n` +
      `Candidate: ${getCandidateId()} | Simulate: ${getSimulateAnomaly() || "none"}`
    );

    if (countdownTimer) clearInterval(countdownTimer);

    countdownTimer = setInterval(async () => {
      if (generation !== currentGeneration || !isAutoMonitoring) {
        clearInterval(countdownTimer); countdownTimer = null; return;
      }
      remainingSec -= 1;
      if (remainingSec > 0) {
        if (countdownDisplay) countdownDisplay.textContent = `Next: ${formatTime(remainingSec)}`;
      } else {
        clearInterval(countdownTimer); countdownTimer = null;
        try {
          await recordClip(25, generation);
          if (isAutoMonitoring && generation === currentGeneration) scheduleNextCycle();
        } catch (error) {
          console.error("[RyouU] Cycle error:", error);
          if (isAutoMonitoring && generation === currentGeneration) {
            setStatus("Cycle error – retrying", "#f59e0b");
            showOutput(`Cycle notice: ${error.message}\nScheduling next cycle…`);
            scheduleNextCycle();
          }
        }
      }
    }, 1000);
  }

  // ── Start / Stop monitoring ───────────────────────────────────────────────
  function startAutoMonitoring() {
    if (!selectedRegion) {
      setStatus("Select candidate region", "#f59e0b");
      showOutput("Click 'Select Region' to choose the candidate's video tile first.");
      return;
    }
    if (!hasUserConsented) {
      if (consentModal) consentModal.classList.remove("hidden");
      return;
    }
    isAutoMonitoring = true;
    currentGeneration += 1;
    setInteractiveDisabled(true);
    if (autoMonitorButton) {
      autoMonitorButton.textContent = "Stop Monitoring";
      autoMonitorButton.classList.add("active-monitoring");
    }
    showOutput(
      `Auto-monitoring started for candidate: ${getCandidateId()}\n` +
      "Records 25s clip at each interval.\n" +
      "Deepfake detection: CLIP ViT-L/14 + Detectif.ai Aletheia\n" +
      "Speaker verification: Detectif.ai Eidos (Supabase/Local)"
    );
    scheduleNextCycle();
  }

  function stopAutoMonitoring() {
    console.log("[RyouU] Stopping monitoring…");
    isAutoMonitoring = false;
    currentGeneration += 1;

    if (countdownTimer)  { clearInterval(countdownTimer);  countdownTimer  = null; }
    if (recordingTimer)  { clearTimeout(recordingTimer);   recordingTimer  = null; }
    if (activeRecorder && activeRecorder.state !== "inactive") {
      try { activeRecorder.stop(); } catch (e) { /* ignore */ }
    }
    stopMicCapture();
    if (activeStream) { activeStream.getTracks().forEach(t => t.stop()); activeStream = null; }
    if (animationFrame !== null) { cancelAnimationFrame(animationFrame); animationFrame = null; }

    currentState = State.IDLE;
    setStatus("Monitoring stopped", "#a78bfa");
    if (countdownDisplay) countdownDisplay.textContent = "Stopped";
    setInteractiveDisabled(false);
    if (autoMonitorButton) {
      autoMonitorButton.textContent = "Start Auto-Monitor";
      autoMonitorButton.classList.remove("active-monitoring");
    }
    setModalityPill(videoBadge,   "ok", "📹 Video: OK");
    setModalityPill(audioBadge,   "ok", "🎙️ Voice: Natural");
    setModalityPill(speakerBadge, "ok", "👤 Speaker: Match");
    showOutput("Auto-monitoring stopped cleanly. No timers or background capture active.");
  }

  // ── Buttons ───────────────────────────────────────────────────────────────
  if (autoMonitorButton) {
    autoMonitorButton.addEventListener("click", () => {
      if (isAutoMonitoring) stopAutoMonitoring();
      else startAutoMonitoring();
    });
  }

  if (consentConfirmBtn) {
    consentConfirmBtn.addEventListener("click", () => {
      hasUserConsented = true;
      if (consentModal) consentModal.classList.add("hidden");
      startAutoMonitoring();
    });
  }

  if (consentCancelBtn) {
    consentCancelBtn.addEventListener("click", () => {
      if (consentModal) consentModal.classList.add("hidden");
      showOutput("Monitoring cancelled. Consent is required before monitoring can begin.");
    });
  }

  if (recordOnceButton) {
    recordOnceButton.addEventListener("click", async () => {
      if (currentState === State.RECORDING || isAutoMonitoring) {
        showOutput("Monitoring or recording is currently active.");
        return;
      }
      if (!selectedRegion) {
        setStatus("Select region first", "#f59e0b");
        showOutput("Click 'Select Region' to select the candidate's video tile first.");
        return;
      }
      if (!hasUserConsented) {
        const consent = window.confirm(
          "Record 25s candidate video + system audio, then verify with CLIP ViT-L/14 and Detectif.ai? Ensure participant consent."
        );
        if (!consent) { showOutput("Manual recording cancelled."); return; }
        hasUserConsented = true;
      }
      try {
        setInteractiveDisabled(true);
        currentGeneration += 1;
        const gen = currentGeneration;
        showOutput(`Recording 25s clip for candidate: ${getCandidateId()}…`);
        await recordClip(25, gen);
        setStatus("Ready", "#22c55e");
        if (countdownDisplay) countdownDisplay.textContent = "Complete";
      } catch (error) {
        console.error("[RyouU] Manual recording failed:", error);
        setStatus("Recording failed", "#ef4444");
        showOutput(error.message || String(error));
      } finally {
        currentState = State.IDLE;
        setInteractiveDisabled(false);
      }
    });
  }

  if (testAudioBtn) {
    testAudioBtn.addEventListener("click", async () => {
      if (currentState === State.RECORDING) {
        showOutput("Recording in progress, please wait.");
        return;
      }
      const candidateId = getCandidateId();
      setInteractiveDisabled(true);
      testAudioBtn.disabled = true;

      setStatus("Recording 25s voice sample…", "#ef4444", true);
      setModalityPill(audioBadge,   "evaluating", "🎙️ Voice: Recording 25s…");
      setModalityPill(speakerBadge, "evaluating", "👤 Speaker: Ready…");
      showOutput(`[Voice Test] Recording fresh 25-second system audio clip to verify against reference 'Test_Audio.m4a' for candidate '${candidateId}'. Please speak clearly…`);

      let micStream = null;
      let countdownTimer = null;

      try {
        micStream = await startMicCapture();
        if (!micStream) {
          throw new Error("System audio capture failed or is unavailable.");
        }

        const audioChunks = [];
        const supportedAudioMime = (() => {
          const types = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/ogg"];
          return types.find(t => MediaRecorder.isTypeSupported(t)) || "";
        })();

        activeAudioRecorder = new MediaRecorder(micStream, supportedAudioMime ? { mimeType: supportedAudioMime } : {});
        activeAudioRecorder.ondataavailable = (e) => {
          if (e.data && e.data.size > 0) audioChunks.push(e.data);
        };

        const totalSecs = 25;
        let remaining = totalSecs;
        if (countdownDisplay) countdownDisplay.textContent = `${remaining}s`;

        countdownTimer = setInterval(() => {
          remaining -= 1;
          if (remaining > 0) {
            if (countdownDisplay) countdownDisplay.textContent = `${remaining}s`;
            setStatus(`Recording Voice: ${remaining}s`, "#ef4444", true);
          } else {
            clearInterval(countdownTimer);
            countdownTimer = null;
            if (countdownDisplay) countdownDisplay.textContent = "Processing…";
          }
        }, 1000);

        activeAudioRecorder.start(250);

        await new Promise((resolve) => {
          setTimeout(() => {
            if (activeAudioRecorder && activeAudioRecorder.state !== "inactive") {
              activeAudioRecorder.onstop = resolve;
              activeAudioRecorder.stop();
            } else {
              resolve();
            }
          }, totalSecs * 1000);
        });

        if (countdownTimer) {
          clearInterval(countdownTimer);
          countdownTimer = null;
        }

        stopMicCapture();

        if (audioChunks.length === 0) {
          throw new Error("No audio data was captured from system audio.");
        }
        if (lastSystemAudioPeak < 0.001) {
          throw new Error(`System audio was silent (peak level ${lastSystemAudioPeak.toFixed(5)}). Make sure the voice is playing through the system's default output device during the 25s recording.`);
        }
        showOutput(`[Voice Test] System audio peak level: ${lastSystemAudioPeak.toFixed(3)}`);

        const actualMime = (activeAudioRecorder && activeAudioRecorder.mimeType) || supportedAudioMime || "audio/webm";
        const audioBlob = new Blob(audioChunks, { type: actualMime });
        const audioBytes = new Uint8Array(await audioBlob.arrayBuffer());

        setStatus("Saving voice clip…", "#a78bfa", true);
        const saveRes = await window.ryouu.saveAudioRecording({ bytes: audioBytes, mimeType: actualMime });
        if (!saveRes || !saveRes.success) {
          throw new Error(`Failed to save audio recording: ${saveRes?.error || "Unknown error"}`);
        }

        showOutput(`[Voice Test] Recorded 25s session clip saved (${saveRes.filePath}). Submitting to Detectif.ai Eidos with reference 'Test_Audio.m4a'…`);
        setStatus("Verifying against reference Test_Audio.m4a…", "#a78bfa", true);
        setModalityPill(audioBadge,   "evaluating", "🎙️ Voice: Analyzing…");
        setModalityPill(speakerBadge, "evaluating", "👤 Speaker: Eidos Verify…");

        const res = await window.ryouu.verifyAudio({
          audioPath: saveRes.filePath,
          candidateId,
          referenceAudioPath: "Test_Audio.m4a",
          mimeType: saveRes.mimeType || actualMime
        });

        handleAudioVerificationResult(res);

        if (res?.speaker_verification?.decision === "match") {
          setStatus("Voice matched", "#22c55e");
        } else if (res?.speaker_verification?.decision === "no_match") {
          setStatus("Voice mismatch flagged", "#ef4444");
        } else if (res?.has_error || !res?.success) {
          setStatus("Verification error", "#ef4444");
        } else {
          setStatus("Ready", "#22c55e");
        }
      } catch (err) {
        console.error("[RyouU] Speaker verification test error:", err);
        setModalityPill(audioBadge,   "error", "🎙️ Voice: Err");
        setModalityPill(speakerBadge, "error", "👤 Speaker: Err");
        setStatus("Test failed", "#ef4444");
        showOutput(`[Voice Test Error] ${err.message || String(err)}`);
      } finally {
        if (countdownTimer) clearInterval(countdownTimer);
        stopMicCapture();
        setInteractiveDisabled(false);
        testAudioBtn.disabled = false;
        if (countdownDisplay) countdownDisplay.textContent = "Idle";
      }
    });
  }

  if (closeButton) {
    closeButton.addEventListener("click", async () => {
      stopAutoMonitoring();
      try { await window.ryouu.closeApp(); }
      catch (e) { console.error("[RyouU] Close error:", e); }
    });
  }

  console.log("[RyouU] All handlers attached (video + audio + Supabase gateway).");
});
