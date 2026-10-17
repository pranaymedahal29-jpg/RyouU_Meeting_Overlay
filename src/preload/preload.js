
const { contextBridge, ipcRenderer } = require("electron");

console.log("[RyouU] Preload loaded");

contextBridge.exposeInMainWorld("ryouu", {
  startDetection: () => ipcRenderer.invoke("detection:start"),
  stopDetection: () => ipcRenderer.invoke("detection:stop"),
  closeApp: () => ipcRenderer.invoke("window:close"),

  openCapturePicker: () =>
    ipcRenderer.invoke("capture:open-picker"),

  selectCaptureRegion: (rect) =>
    ipcRenderer.invoke("capture:select-region", rect),

  cancelCaptureRegion: () =>
    ipcRenderer.invoke("capture:cancel-region"),

  getRecordingSource: () =>
    ipcRenderer.invoke("recording:get-source"),

  saveRecording: (bytes) =>
    ipcRenderer.invoke("recording:save-video", bytes),

  saveAudioRecording: (bytes) =>
    ipcRenderer.invoke("recording:save-audio", bytes),

  verifyDeepfake: (filePath) =>
    ipcRenderer.invoke("recording:verify-deepfake", filePath),

  verifyAudio: (options) =>
    ipcRenderer.invoke("audio:verify", options),

  testSupabaseConnection: () =>
    ipcRenderer.invoke("supabase:test-connection"),

  listReferenceAudio: () =>
    ipcRenderer.invoke("supabase:list-audio"),

  fetchReferenceAudio: (candidateId) =>
    ipcRenderer.invoke("supabase:fetch-audio", candidateId),

  onDeepfakeFlag: (callback) => {
    ipcRenderer.on("deepfake:flag-result", (_event, result) => {
      callback(result);
    });
  },

  onAudioFlag: (callback) => {
    ipcRenderer.on("audio:flag-result", (_event, result) => {
      callback(result);
    });
  },

  onZoomStatus: (callback) => {
    ipcRenderer.on("detection:zoom-status", (_event, detected) => {
      callback(detected);
    });
    console.log("[RyouU] Listening for Zoom status");
  },

  onRegionSelected: (callback) => {
    ipcRenderer.on("capture:region-selected", (_event, region) => {
      console.log("[RyouU] Region event received in preload", region);
      callback(region);
    });
    console.log("[RyouU] Listening for region selection");
  }
});
