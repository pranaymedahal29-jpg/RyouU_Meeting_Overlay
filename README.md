# RyouU Overlay

Standalone desktop overlay for remote interview integrity monitoring — **video deepfake detection, voice deepfake detection, and speaker verification**, with Supabase KYC reference audio integration.

---

## Features

### 🎥 Auto-Record Scheduler
- Records a **25-second video clip** of the selected candidate tile at configurable intervals:
  - **1 Minute (60s)** — production default
  - **2–5 Min (Random)** — randomised stealth interval
  - **10s (Test)** — rapid testing
- Explicit participant consent gate before monitoring begins
- Live countdown timer (`Next: 0:54` / `Rec: 18s`)
- Clean stop with zero dangling timers or background capture

### 📹 Video Deepfake Detection — CLIP ViT-L/14
- Extracts keyframes from every 25-second clip
- Zero-shot contrastive evaluation via `openai/clip-vit-large-patch14`
- Contrasts authentic webcam face prompts against synthetic/deepfake prompts
- **Categorization Threshold**: Enforces a strict **70% confidence threshold** (`real_confidence >= 0.70` & `median_real_prob >= 0.65`) to categorize the video as real; clips falling below 70% real confidence trigger an integrity flag
- Strict error-based architecture with zero fallbacks: model failures skip verification gracefully without generating false alarms

### 🎙️ Voice Deepfake Detection — Detectif.ai Aletheia
- Official route: `POST https://platform.detectif.ai/v1/deepfake/detect`
- Uploads audio via multipart `file` parameter (up to 100 MB)
- Header authentication with `Authorization: Bearer <DETECTIF_API_KEY>` and `Idempotency-Key: <uuid>`
- Checks whether recordings contain AI-generated speech (`verdict: "real"` / `"fake"` / `"not_sure"`)
- Automatic local acoustic analysis fallback when offline or debugging

### 👤 Speaker Verification — Detectif.ai Eidos + Supabase Gateway
- Official routes:
  - `POST https://platform.detectif.ai/v1/speakers/enroll` (enrolls KYC reference audio)
  - `POST https://platform.detectif.ai/v1/speakers/verify` (verifies session audio against candidate voiceprint)
  - `GET https://platform.detectif.ai/v1/speakers/{id}` (checks enrollment status)
- Reference audio is fetched from the Supabase Storage / Database gateway
- Automatic local cosine-similarity voice biometrics fallback against `debug_audio/`

### 🚩 Multi-Modal Flag Alerts
- **Modality status pills** show live status for each check: Video / Voice / Speaker
- **Flag Alert Banner** appears prominently when any anomaly is detected — shows model, score, and specific reasons
- **Flag counter badge** tracks cumulative suspicious clips per session
- **Verified badge** displays `✓ Integrity Verified` when all checks pass
- **Simulate mode** lets you trigger `Voice Clone` or `Speaker Mismatch` flags without real anomalies (debugging)

---

## Architecture

```
RyouU_Overlay/
├── .env.example                  ← Environment config template
├── debug_audio/                  ← Local reference WAV samples (see Local Debug)
│   ├── candidate-001_reference.wav
│   └── candidate-002_impostor.wav
├── storage/
│   └── reference_cache/          ← Supabase-downloaded files cached here
└── src/
    ├── main/
    │   ├── main.js               ← Electron main process + all IPC handlers
    │   ├── verify_deepfake.py    ← CLIP ViT-L/14 video analysis
    │   ├── detectifai_verifier.py← Detectif.ai Aletheia + Eidos + Supabase gateway
    │   └── supabase_gateway.py   ← Supabase Storage/DB fetch + local fallback
    ├── preload/
    │   └── preload.js            ← Secure contextBridge API
    └── renderer/
        ├── index.html            ← Overlay UI
        ├── renderer.js           ← Auto-record state machine + audio pipeline
        ├── styles.css            ← Glassmorphic UI + modality pills
        ├── picker.html/.js/.css  ← Region selector
```

---

## Getting Started

### Prerequisites

| Requirement | Version |
|-------------|---------|
| Node.js | v18+ |
| Python | 3.10+ |
| PyTorch CPU | `torch==2.14+cpu` |
| transformers | 5.x |
| scipy, numpy | any recent |
| opencv-python | 4.x / 5.x |
| python-dotenv | any |

Install Python dependencies:
```powershell
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install transformers scipy opencv-python pillow python-dotenv
```

### Launch the Application
```powershell
npm.cmd start
```

---

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```ini
# Detectif.ai API key (from https://platform.detectif.ai/dashboard/)
DETECTIFAI_API_KEY=your_key_here

# Supabase project settings
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_anon_key_here
SUPABASE_BUCKET=kyc-reference-audio   # Storage bucket name
SUPABASE_TABLE=candidates              # Table with candidate records
```

**None of these are required for local debug mode.** The app works out-of-the-box using local WAV files in `debug_audio/`.

---

## Supabase Audio Gateway

### How it works

The `supabase_gateway.py` module resolves reference audio for speaker verification using this priority chain:

```
1. Explicit --local-audio override path (debug CLI flag)
       ↓ (not set)
2. Supabase REST API  →  find candidate row in <SUPABASE_TABLE>
   Supabase Storage  →  download <candidate_id>.wav from <SUPABASE_BUCKET>
       ↓ (Supabase not configured or file not found)
3. Local debug_audio/<candidate_id>_reference.wav
   Local debug_audio/<first .wav found>
```

### Setting up Supabase

1. Create a **Storage bucket** named `kyc-reference-audio` (or your custom `SUPABASE_BUCKET`)
2. Upload reference WAV files as `<candidate_id>.wav` (e.g. `candidate-001.wav`)
3. Optionally create a `candidates` table with columns:
   - `candidate_id` (text, primary key)
   - `reference_audio_path` (text) — path within the bucket, or a full URL
4. Set bucket policy to **authenticated** (recommended) — the gateway uses your service key
5. Add `SUPABASE_URL` and `SUPABASE_KEY` to `.env`

### Local Debug Mode

Drop WAV files into `debug_audio/` using the naming convention:

```
debug_audio/
  candidate-001_reference.wav   ← used when candidate ID = "candidate-001"
  candidate-002_reference.wav   ← used when candidate ID = "candidate-002"
  default_reference.wav         ← ultimate fallback for any unknown ID
```

The gateway badge in the overlay shows `⚡ Gateway: Local Debug` when Supabase is not configured. **Click the badge** to see a full inventory of available audio files.

---

## Usage Workflow

1. **Select Region** — drag a rectangle over the candidate's video tile in Zoom / Meet
2. **Set Candidate ID** — type the candidate's ID to match against their Supabase reference audio (default: `candidate-001`)
3. **Set Interval** — choose `1 Min`, `2–5 Min (Random)`, or `10s (Test)`
4. **Optionally set Simulate** — `Voice Clone` or `Speaker Mismatch` to test the flag UI
5. **Start Auto-Monitor** — confirm consent, monitoring begins
6. At each interval:
   - 25-second video clip is recorded → CLIP ViT-L/14 deepfake analysis
   - Microphone audio is recorded in parallel → Detectif.ai Aletheia (voice deepfake) + Eidos (speaker verification against Supabase/local reference)
   - Clips saved to `%USERPROFILE%\Videos\RyouU Recordings`
7. **Flag banners** appear if any check triggers. The three modality pills update in real-time
8. **Stop Monitoring** at any time — all timers, streams, and background processes stop cleanly

---

## Debug Simulation

In the **Simulate** dropdown while Supabase/Detectif.ai are not configured:

| Option | Behaviour |
|--------|-----------|
| `None` | Normal local biometric analysis |
| `Voice Clone` | Forces `is_deepfake = true` with 91% synthetic score |
| `Speaker Mismatch` | Forces `is_match = false` with 23% match score |

---

## Recordings

Videos and audio are saved to:
```
%USERPROFILE%\Videos\RyouU Recordings\
  candidate-2026-10-17T10-30-00-000Z.webm        ← video clip
  candidate-2026-10-17T10-30-00-000Z-audio.wav   ← audio clip
```

---

## Security Notes

- **No network transmission** of video or audio — all verification runs locally or via your own API key
- Desktop capture and microphone access require explicit user permission from the OS
- Supabase key is stored in `.env` (server-side only, never in the renderer)
- Electron uses `contextIsolation: true`, `nodeIntegration: false`, and a narrow `contextBridge` API


## Replace Test_Audio.m4a and Test_Audio.mp4 with custom audio to test Eidos Model