
const {
  app,
  BrowserWindow,
  ipcMain,
  screen,
  desktopCapturer,
  session
} = require("electron");

const path = require("path");
const fs = require("fs");
const { execFile } = require("child_process");

let mainWindow = null;
let regionSelectorWindow = null;
let detectionTimer = null;
let zoomDetected = false;
let selectedRegion = null;

function log(message, data) {
  console.log(
    `[RyouU ${new Date().toLocaleTimeString()}] ${message}`,
    data ?? ""
  );
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 410,
    height: 420,
    minWidth: 410,
    maxWidth: 410,
    minHeight: 420,
    maxHeight: 420,
    frame: false,
    transparent: true,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    webPreferences: {
      preload: path.join(__dirname, "../preload/preload.js"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  mainWindow.setAlwaysOnTop(true, "screen-saver");
  mainWindow.setVisibleOnAllWorkspaces(true);

  mainWindow.webContents.on("console-message", (event) => {
    log(`RENDERER: ${event.message}`);
  });

  mainWindow.webContents.on("did-fail-load", (_event, code, description) => {
    log(`ERROR: Overlay failed to load: ${description} (${code})`);
  });

  mainWindow.loadFile(path.join(__dirname, "../renderer/index.html"));

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

function checkZoomRunning() {
  execFile(
    "tasklist",
    ["/FI", "IMAGENAME eq Zoom.exe", "/NH"],
    { windowsHide: true },
    (error, stdout) => {
      if (error) {
        log("Zoom check failed", error.message);
        return;
      }

      const detected = stdout.toLowerCase().includes("zoom.exe");

      if (detected !== zoomDetected) {
        zoomDetected = detected;
        log(detected ? "Zoom process detected" : "Zoom process not detected");

        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.send("detection:zoom-status", detected);
        }
      }
    }
  );
}

function startZoomDetection() {
  if (detectionTimer) {
    log("Zoom detection already running");
    return;
  }

  log("Starting Zoom detection");
  checkZoomRunning();
  detectionTimer = setInterval(checkZoomRunning, 3000);
}

function stopZoomDetection() {
  if (detectionTimer) {
    clearInterval(detectionTimer);
    detectionTimer = null;
    log("Zoom detection stopped");
  }
}

function openRegionSelector() {
  if (regionSelectorWindow && !regionSelectorWindow.isDestroyed()) {
    regionSelectorWindow.focus();
    return;
  }

  const display = screen.getPrimaryDisplay();
  const bounds = display.bounds;

  log("Opening region selector", bounds);

  regionSelectorWindow = new BrowserWindow({
    x: bounds.x,
    y: bounds.y,
    width: bounds.width,
    height: bounds.height,
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    resizable: false,
    movable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    hasShadow: false,
    webPreferences: {
      preload: path.join(__dirname, "../preload/preload.js"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  regionSelectorWindow.setAlwaysOnTop(true, "screen-saver");
  regionSelectorWindow.setVisibleOnAllWorkspaces(true);

  regionSelectorWindow.loadFile(
    path.join(__dirname, "../renderer/picker.html")
  );

  regionSelectorWindow.webContents.on("did-finish-load", () => {
    log("Region selector loaded");
  });

  regionSelectorWindow.on("closed", () => {
    regionSelectorWindow = null;
    log("Region selector closed");
  });
}

ipcMain.handle("detection:start", async () => {
  log("Received Start button request");
  startZoomDetection();
  return { success: true };
});

ipcMain.handle("detection:stop", async () => {
  stopZoomDetection();
  return { success: true };
});

ipcMain.handle("window:close", async () => {
  app.quit();
  return { success: true };
});

ipcMain.handle("capture:open-picker", async () => {
  log("Received Select button request");
  openRegionSelector();
  return { success: true };
});

ipcMain.handle("capture:select-region", async (_event, rect) => {
  log("Received selection coordinates", rect);

  if (!rect || typeof rect !== "object") {
    return { success: false, error: "Invalid selection." };
  }

  const x = Number(rect.x);
  const y = Number(rect.y);
  const width = Number(rect.width);
  const height = Number(rect.height);

  if (
    ![x, y, width, height].every(Number.isFinite) ||
    width < 40 ||
    height < 40
  ) {
    return { success: false, error: "Selection is too small or invalid." };
  }

  const display = screen.getPrimaryDisplay();

  // Coordinates are logical pixels relative to the primary display.
  const maxWidth = display.size.width;
  const maxHeight = display.size.height;

  const left = Math.max(0, Math.min(x, maxWidth));
  const top = Math.max(0, Math.min(y, maxHeight));
  const right = Math.max(left, Math.min(x + width, maxWidth));
  const bottom = Math.max(top, Math.min(y + height, maxHeight));

  if (right - left < 40 || bottom - top < 40) {
    return { success: false, error: "Selection is outside the display." };
  }

  selectedRegion = {
    x: left,
    y: top,
    width: right - left,
    height: bottom - top,
    displayId: String(display.id),
    scaleFactor: display.scaleFactor,
    displayWidth: display.size.width,
    displayHeight: display.size.height
  };

  log("SUCCESS: Candidate region selected", selectedRegion);

  if (regionSelectorWindow && !regionSelectorWindow.isDestroyed()) {
    regionSelectorWindow.close();
  }

  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.show();
    mainWindow.setAlwaysOnTop(true, "screen-saver");
    mainWindow.webContents.send("capture:region-selected", selectedRegion);
    log("Sent selected region to overlay");
  }

  return { success: true, region: selectedRegion };
});

ipcMain.handle("capture:cancel-region", async () => {
  if (regionSelectorWindow && !regionSelectorWindow.isDestroyed()) {
    regionSelectorWindow.close();
  }

  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.show();
    mainWindow.setAlwaysOnTop(true, "screen-saver");
  }

  log("Region selection cancelled");
  return { success: true };
});

// Return the screen source and selection details to the renderer.
ipcMain.handle("recording:get-source", async () => {
  if (!selectedRegion) {
    return {
      success: false,
      error: "Select the candidate's video tile first."
    };
  }

  const sources = await desktopCapturer.getSources({
    types: ["screen"],
    thumbnailSize: { width: 1, height: 1 },
    fetchWindowIcons: false
  });

  const source = sources.find(
    (item) => String(item.display_id) === selectedRegion.displayId
  );

  if (!source) {
    log("ERROR: Could not find selected display source", {
      requestedDisplay: selectedRegion.displayId,
      availableSources: sources.map((item) => ({
        name: item.name,
        display_id: item.display_id
      }))
    });

    return {
      success: false,
      error: "Could not find the selected display for recording."
    };
  }

  log("Screen source found", {
    name: source.name,
    displayId: source.display_id
  });

  return {
    success: true,
    sourceId: source.id,
    region: selectedRegion
  };
});

// Save the finished WebM video locally.
ipcMain.handle("recording:save-video", async (_event, bytes) => {
  try {
    if (!bytes || !ArrayBuffer.isView(bytes)) {
      throw new Error("The recorded video data is invalid.");
    }

    const recordingsDir = path.join(app.getPath("videos"), "RyouU Recordings");

    fs.mkdirSync(recordingsDir, { recursive: true });

    const timestamp = new Date()
      .toISOString()
      .replace(/[:.]/g, "-");

    const filePath = path.join(
      recordingsDir,
      `candidate-${timestamp}.webm`
    );

    fs.writeFileSync(filePath, Buffer.from(bytes));

    const stats = fs.statSync(filePath);

    log("VIDEO SAVED SUCCESSFULLY", {
      filePath,
      bytes: stats.size
    });

    return {
      success: true,
      filePath,
      size: stats.size
    };
  } catch (error) {
    log("ERROR: Could not save video", error.message);

    return {
      success: false,
      error: error.message
    };
  }
});

// Run CLIP ViT-L/14 verification on the recorded video
ipcMain.handle("recording:verify-deepfake", async (_event, filePath) => {
  log("Starting deepfake verification for:", filePath);

  if (!filePath || typeof filePath !== "string" || !fs.existsSync(filePath)) {
    const errRes = {
      success: false,
      skipped: true,
      error_code: "FILE_NOT_FOUND",
      error: "Invalid or missing video file path for verification."
    };
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("deepfake:flag-result", errRes);
    }
    return errRes;
  }

  return new Promise((resolve) => {
    const scriptPath = path.join(__dirname, "verify_deepfake.py");
    const pythonExe = process.platform === "win32" ? "python" : "python3";

    execFile(
      pythonExe,
      [scriptPath, "--video", filePath],
      { windowsHide: true, maxBuffer: 10 * 1024 * 1024 },
      (error, stdout, stderr) => {
        try {
          const trimmed = stdout.trim();
          const jsonStart = trimmed.indexOf("{");
          const jsonEnd = trimmed.lastIndexOf("}");

          if (jsonStart !== -1 && jsonEnd !== -1) {
            const parsed = JSON.parse(trimmed.slice(jsonStart, jsonEnd + 1));
            
            // If the model reported an error or failure, mark as skipped
            if (!parsed.success) {
              parsed.skipped = true;
            }

            log("Deepfake verification result:", {
              success: parsed.success,
              skipped: parsed.skipped,
              anomaly_score: parsed.anomaly_score,
              is_anomaly: parsed.is_anomaly,
              error: parsed.error
            });

            if (mainWindow && !mainWindow.isDestroyed()) {
              mainWindow.webContents.send("deepfake:flag-result", parsed);
            }

            resolve(parsed);
          } else {
            throw new Error(stdout || stderr || "No valid JSON output from CLIP analyzer.");
          }
        } catch (parseErr) {
          log("CLIP verification failed / skipped:", parseErr.message);
          const errRes = {
            success: false,
            skipped: true,
            error_code: "CLIP_MODEL_FAILED",
            error: parseErr.message,
            rawOutput: stdout
          };

          if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send("deepfake:flag-result", errRes);
          }

          resolve(errRes);
        }
      }
    );
  });
});

// Save session audio recording locally preserving container MIME and extension
ipcMain.handle("recording:save-audio", async (_event, payload, mimeTypeArg) => {
  try {
    let bytes = payload;
    let mimeType = typeof mimeTypeArg === "string" ? mimeTypeArg : "";

    if (payload && payload.bytes) {
      bytes = payload.bytes;
      mimeType = payload.mimeType || mimeType;
    }

    if (!bytes || !ArrayBuffer.isView(bytes)) {
      throw new Error("Invalid audio buffer.");
    }

    const recordingsDir = path.join(app.getPath("videos"), "RyouU Recordings");
    fs.mkdirSync(recordingsDir, { recursive: true });

    const buf = Buffer.from(bytes);
    let ext = ".webm";
    let detectedMime = mimeType || "audio/webm";

    if (buf.length >= 4) {
      if (buf[0] === 0x1A && buf[1] === 0x45 && buf[2] === 0xDF && buf[3] === 0xA3) {
        ext = ".webm";
        detectedMime = "audio/webm";
      } else if (buf.slice(0, 4).toString() === "OggS") {
        ext = ".ogg";
        detectedMime = "audio/ogg";
      } else if (buf.length >= 12 && buf.slice(0, 4).toString() === "RIFF" && buf.slice(8, 12).toString() === "WAVE") {
        ext = ".wav";
        detectedMime = "audio/wav";
      } else if (mimeType.includes("ogg") || mimeType.includes("opus")) {
        ext = ".ogg";
        detectedMime = "audio/ogg";
      } else if (mimeType.includes("webm")) {
        ext = ".webm";
        detectedMime = "audio/webm";
      } else if (mimeType.includes("wav")) {
        ext = ".wav";
        detectedMime = "audio/wav";
      }
    }

    const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
    const filePath = path.join(recordingsDir, `candidate-${timestamp}-audio${ext}`);

    fs.writeFileSync(filePath, buf);
    const stats = fs.statSync(filePath);

    log("AUDIO SAVED SUCCESSFULLY", { filePath, bytes: stats.size, mimeType: detectedMime, ext });
    return { success: true, filePath, mimeType: detectedMime, extension: ext, size: stats.size };
  } catch (error) {
    log("ERROR saving audio:", error.message);
    return { success: false, error: error.message };
  }
});

// Run Detectif.ai Audio Verification (Deepfake + Speaker via Supabase)
ipcMain.handle("audio:verify", async (_event, options) => {
  let { audioPath, candidateId, referenceAudioPath, simulateAnomaly, mimeType } = options || {};
  const projectRoot = path.join(__dirname, "..", "..");

  if (audioPath && !path.isAbsolute(audioPath)) {
    const resolvedPath = path.join(projectRoot, audioPath);
    if (fs.existsSync(resolvedPath)) {
      audioPath = resolvedPath;
    }
  }

  // Reference (real voice) is always Test_Audio.m4a in the project root unless explicitly overridden.
  // Always resolve to an absolute path so a missing file is reported clearly instead of silently
  // falling back to a different reference.
  if (!referenceAudioPath) {
    referenceAudioPath = "Test_Audio.m4a";
  }
  if (!path.isAbsolute(referenceAudioPath)) {
    referenceAudioPath = path.join(projectRoot, referenceAudioPath);
  }

  log("Starting Detectif.ai audio verification for:", { audioPath, candidateId, referenceAudioPath, mimeType });

  if (!audioPath || !fs.existsSync(audioPath)) {
    return { success: false, error: `Audio file path is missing or invalid: ${audioPath}` };
  }

  return new Promise((resolve) => {
    const scriptPath = path.join(__dirname, "detectifai_verifier.py");
    const pythonExe = process.platform === "win32" ? "python" : "python3";
    const args = [
      scriptPath,
      "--audio", audioPath,
      "--candidate-id", candidateId || "candidate-001"
    ];

    if (referenceAudioPath) {
      args.push("--reference-audio", referenceAudioPath);
    }
    if (simulateAnomaly && simulateAnomaly !== "none" && simulateAnomaly !== "test_audio") {
      args.push("--simulate-anomaly", simulateAnomaly);
    }
    if (mimeType) {
      args.push("--mime-type", mimeType);
    }

    execFile(
      pythonExe,
      args,
      { windowsHide: true, maxBuffer: 10 * 1024 * 1024 },
      (error, stdout, stderr) => {
        if (error) {
          log("Audio verification process note:", error.message);
          if (stderr) log("stderr:", stderr);
        }

        try {
          const trimmed = stdout.trim();
          const jsonStart = trimmed.indexOf("{");
          const jsonEnd = trimmed.lastIndexOf("}");

          if (jsonStart !== -1 && jsonEnd !== -1) {
            const parsed = JSON.parse(trimmed.slice(jsonStart, jsonEnd + 1));
            log("Detectif.ai audio verification result:", {
              cloud_verified: parsed.cloud_verified,
              overall_status: parsed.overall_status,
              has_audio_anomaly: parsed.has_audio_anomaly,
              has_inconclusive: parsed.has_inconclusive,
              has_error: parsed.has_error,
              deepfake_verdict: parsed.audio_deepfake?.verdict,
              speaker_decision: parsed.speaker_verification?.decision
            });

            resolve(parsed);
          } else {
            throw new Error(stdout || stderr || "No valid JSON output from audio verifier.");
          }
        } catch (parseErr) {
          log("Failed to parse audio verification result:", parseErr.message);
          const errRes = {
            success: false,
            cloud_verified: false,
            overall_status: "error",
            has_error: true,
            error_code: "parse_error",
            error: parseErr.message,
            rawOutput: stdout
          };
          resolve(errRes);
        }
      }
    );
  });
});

// Test connection to Supabase database & storage
ipcMain.handle("supabase:test-connection", async () => {
  return new Promise((resolve) => {
    const scriptPath = path.join(__dirname, "supabase_gateway.py");
    const pythonExe = process.platform === "win32" ? "python" : "python3";

    execFile(
      pythonExe,
      [scriptPath, "--test-connection"],
      { windowsHide: true },
      (error, stdout) => {
        try {
          const trimmed = stdout.trim();
          const parsed = JSON.parse(trimmed.slice(trimmed.indexOf("{"), trimmed.lastIndexOf("}") + 1));
          resolve(parsed);
        } catch (e) {
          resolve({ success: false, configured: false, error: e.message });
        }
      }
    );
  });
});

// List available reference audio files (from Supabase bucket or local debug dir)
ipcMain.handle("supabase:list-audio", async () => {
  return new Promise((resolve) => {
    const scriptPath = path.join(__dirname, "supabase_gateway.py");
    const pythonExe = process.platform === "win32" ? "python" : "python3";

    execFile(
      pythonExe,
      [scriptPath, "--list"],
      { windowsHide: true },
      (error, stdout) => {
        try {
          const trimmed = stdout.trim();
          const parsed = JSON.parse(trimmed.slice(trimmed.indexOf("{"), trimmed.lastIndexOf("}") + 1));
          resolve(parsed);
        } catch (e) {
          resolve({ success: false, error: e.message });
        }
      }
    );
  });
});

// Fetch reference audio from Supabase Gateway for a candidate
ipcMain.handle("supabase:fetch-audio", async (_event, candidateId) => {
  return new Promise((resolve) => {
    const scriptPath = path.join(__dirname, "supabase_gateway.py");
    const pythonExe = process.platform === "win32" ? "python" : "python3";

    execFile(
      pythonExe,
      [scriptPath, "--candidate-id", candidateId || "candidate-001", "--fetch"],
      { windowsHide: true },
      (error, stdout) => {
        try {
          const trimmed = stdout.trim();
          const parsed = JSON.parse(trimmed.slice(trimmed.indexOf("{"), trimmed.lastIndexOf("}") + 1));
          resolve(parsed);
        } catch (e) {
          resolve({ success: false, error: e.message });
        }
      }
    );
  });
});

app.whenReady().then(() => {
  log("Electron app started");
  log(`Main process directory: ${__dirname}`);

  // System-level audio only: getDisplayMedia returns loopback of audio being played by the
  // system (i.e. the audio received from the meeting), never the microphone.
  session.defaultSession.setDisplayMediaRequestHandler(async (_request, callback) => {
    try {
      const sources = await desktopCapturer.getSources({ types: ["screen"] });
      callback({ video: sources[0], audio: "loopback" });
    } catch (err) {
      log("ERROR: system audio loopback request failed:", err.message);
      callback({});
    }
  });

  createMainWindow();
  startZoomDetection();
});

app.on("before-quit", () => {
  stopZoomDetection();
  log("Shutting down RyouU");
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});
