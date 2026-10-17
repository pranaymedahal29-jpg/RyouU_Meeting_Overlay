"""
Detectif.ai Audio Verification Engine for RyouU.

Strict, fail-closed implementation faithful to the official Detectif.ai API contract.
Source of Truth: https://platform.detectif.ai/docs/api.md and OpenAPI 3.1.0 specification.

Requirements Enforced:
1. Documented Routes & Multipart format:
   - Aletheia: POST /v1/deepfake/detect, multipart field 'file'
   - Eidos enrollment: POST /v1/speakers/enroll, multipart field 'file' + 'speaker_id'
   - Eidos verification: POST /v1/speakers/verify, multipart field 'file' + 'speaker_id'
   - Bearer authentication and preservation of request IDs & vendor error codes
2. Fail-Closed Architecture:
   - No silent substitution of local heuristics for cloud checks.
   - Missing credentials, request failures, HTTP errors, failed enrollment, or malformed responses
     return explicit failure/indeterminate results.
   - Never report overall cloud verification unless both required cloud checks completed with valid responses.
   - Local debug mode is explicitly labeled as local-only and never treated as a Detectif.ai verified pass.
3. Decision Field Fidelity:
   - Aletheia: Uses result.verdict ('real', 'fake', 'not_sure') as the sole decider.
     'not_sure' is treated as inconclusive, never 'Natural' or pass.
   - Eidos: Uses result.decision ('match', 'no_match') as the sole decider.
     Similarity ranges from -1 to 1 and is not a percentage.
   - Malformed/contradictory payloads are rejected.
4. Audio Format Integrity:
   - Preserves actual container MIME type (WebM, OGG, WAV, MP3, FLAC) and extension.
   - Compressed bytes are never labeled as WAV.
   - Local WAV analysis requires genuine RIFF WAV and rejects invalid/container audio.
5. Speaker ID Validation:
   - Strictly validates candidate_id against ^[A-Za-z0-9_.:-]{1,64}$. Does not silently mutate IDs.
   - Only verifies after speaker exists or reference sample is confirmed enrolled.
"""

import os
import sys
import re
import json
import uuid
import argparse
import traceback
import shutil
import tempfile
import hashlib
import subprocess
from pathlib import Path
import numpy as np

# Load .env if present (only when running as script, never during test mocks)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests
from scipy.io import wavfile
import scipy.signal

# Set stdout encoding to utf-8 on Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# Import Supabase Audio Gateway
try:
    from supabase_gateway import SupabaseAudioGateway
except ImportError:
    from src.main.supabase_gateway import SupabaseAudioGateway

SPEAKER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def validate_speaker_id(speaker_id):
    """
    Validates speaker identifier strictly against Detectif.ai schema:
    ^[A-Za-z0-9_.:-]{1,64}$
    Raises ValueError on invalid ID. Never silently mutates invalid IDs.
    """
    if not isinstance(speaker_id, str) or not speaker_id:
        raise ValueError("speaker_id must be a non-empty string between 1 and 64 characters.")
    if not SPEAKER_ID_PATTERN.match(speaker_id):
        raise ValueError(
            f"Invalid speaker_id '{speaker_id}': must be 1 to 64 characters matching pattern ^[A-Za-z0-9_.:-]{{1,64}}$"
        )
    return speaker_id


def detect_audio_mime_and_ext(file_path, explicit_mime=None):
    """
    Inspects magic bytes and file extension to determine the genuine MIME type and extension.
    Never labels compressed bytes (e.g. WebM/Opus or Ogg) as WAV.
    """
    path_obj = Path(file_path)
    ext = path_obj.suffix.lower()

    # Read first 16 bytes for container signature
    magic = b""
    if os.path.isfile(file_path):
        try:
            with open(file_path, "rb") as f:
                magic = f.read(16)
        except Exception:
            pass

    # 1. Matroska / WebM: starts with 0x1A 0x45 0xDF 0xA3
    if len(magic) >= 4 and magic[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm", ".webm"

    # 2. Ogg container (Opus / Vorbis): starts with OggS
    if len(magic) >= 4 and magic[:4] == b"OggS":
        return "audio/ogg", ".ogg"

    # 3. RIFF WAV: starts with RIFF and has WAVE at offset 8
    if len(magic) >= 12 and magic[:4] == b"RIFF" and magic[8:12] == b"WAVE":
        return "audio/wav", ".wav"

    # 4. FLAC: starts with fLaC
    if len(magic) >= 4 and magic[:4] == b"fLaC":
        return "audio/flac", ".flac"

    # 5. MP3: ID3 tag or MPEG sync word 0xFF 0xFB / 0xFF 0xF3
    if len(magic) >= 3 and (magic[:3] == b"ID3" or (magic[0] == 0xFF and (magic[1] & 0xE0) == 0xE0)):
        return "audio/mpeg", ".mp3"

    # 6. MP4 / M4A (ISO base media container): offset 4..8 is 'ftyp'
    if len(magic) >= 8 and magic[4:8] == b"ftyp":
        if ext == ".m4a":
            return "audio/mp4", ".m4a"
        return "video/mp4", ".mp4"

    # Fallback to explicit MIME if supplied
    if explicit_mime:
        em = explicit_mime.lower()
        if "webm" in em:
            return "audio/webm", ".webm"
        if "ogg" in em or "opus" in em:
            return "audio/ogg", ".ogg"
        if "wav" in em:
            return "audio/wav", ".wav"
        if "mp3" in em or "mpeg" in em:
            return "audio/mpeg", ".mp3"
        if "flac" in em:
            return "audio/flac", ".flac"
        if "m4a" in em:
            return "audio/mp4", ".m4a"
        if "mp4" in em:
            return "video/mp4", ".mp4"

    # Fallback to extension
    ext_map = {
        ".webm": ("audio/webm", ".webm"),
        ".ogg": ("audio/ogg", ".ogg"),
        ".opus": ("audio/ogg", ".ogg"),
        ".wav": ("audio/wav", ".wav"),
        ".mp3": ("audio/mpeg", ".mp3"),
        ".flac": ("audio/flac", ".flac"),
        ".m4a": ("audio/mp4", ".m4a"),
        ".mp4": ("video/mp4", ".mp4"),
        ".aac": ("audio/aac", ".aac"),
        ".aiff": ("audio/aiff", ".aiff"),
    }
    return ext_map.get(ext, ("audio/octet-stream", ext or ".bin"))


def _get_mp4_duration(data):
    """
    Parses duration in seconds from MP4/M4A moov->mvhd box.
    Returns float duration in seconds, or None if mvhd not found/unparseable.
    """
    idx = data.find(b"mvhd")
    if idx == -1 or idx + 24 > len(data):
        return None
    try:
        version = data[idx + 4]
        if version == 0:
            timescale = int.from_bytes(data[idx + 16 : idx + 20], "big")
            duration = int.from_bytes(data[idx + 20 : idx + 24], "big")
        elif version == 1:
            timescale = int.from_bytes(data[idx + 24 : idx + 28], "big")
            duration = int.from_bytes(data[idx + 28 : idx + 36], "big")
        else:
            return None
        if timescale > 0:
            return duration / timescale
    except Exception:
        return None
    return None


def _get_wav_duration(file_path):
    """
    Returns duration in seconds for a RIFF WAV file, or None if unparseable.
    """
    try:
        import wave
        with wave.open(str(file_path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate > 0:
                return frames / float(rate)
    except Exception:
        pass
    return None


def has_mp4_audio_stream(file_path):
    """
    Inspects an MP4 container file to verify if it contains an audio track.
    Looks for the 'soun' handler box or standard audio codec fourcc identifiers (mp4a, alac, ac-3, etc.).
    """
    path_obj = Path(file_path)
    if not path_obj.is_file():
        return False
    size = path_obj.stat().st_size
    if size == 0:
        return False
    try:
        with open(path_obj, "rb") as f:
            data = f.read(min(size, 8 * 1024 * 1024))
        audio_signatures = [b"soun", b"mp4a", b"alac", b"ac-3", b"ec-3", b"Opus", b"fLaC"]
        return any(sig in data for sig in audio_signatures)
    except Exception:
        return False


def extract_audio_from_mp4(mp4_path, output_path=None):
    """
    Extracts the audio track from an MP4 file into a 16kHz mono 16-bit PCM WAV file using FFmpeg.
    Verifies that the MP4 has an audio track before extraction and verifies that the output
    WAV file was successfully created and is valid.

    Returns:
        str: Absolute path to the extracted WAV file.
    Raises:
        ValueError: If file not found, empty, or contains no usable audio track.
        RuntimeError: If FFmpeg is missing or extraction fails.
    """
    path_obj = Path(mp4_path)
    if not path_obj.is_file():
        raise ValueError(f"MP4 file not found: {mp4_path}")
    if path_obj.stat().st_size == 0:
        raise ValueError(f"MP4 file is empty (0 bytes): {mp4_path}")

    # 1. Verify that the file has an audio stream
    if not has_mp4_audio_stream(path_obj):
        raise ValueError(f"Reference MP4 file '{path_obj.name}' contains no usable audio stream.")

    # 2. Locate ffmpeg
    ffmpeg_bin = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError(
            "FFmpeg is required to extract audio from MP4 files but was not found on PATH. "
            "Please install FFmpeg or provide reference audio in a supported format (WAV, MP3, FLAC, OGG, WebM, M4A)."
        )

    if output_path:
        out_wav = Path(output_path)
    else:
        fd, temp_path = tempfile.mkstemp(prefix="ref_extracted_", suffix=".wav")
        os.close(fd)
        out_wav = Path(temp_path)

    cmd = [
        ffmpeg_bin,
        "-y",
        "-i", str(path_obj.resolve()),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(out_wav.resolve())
    ]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except Exception as exec_err:
        if out_wav.exists():
            try: os.remove(str(out_wav))
            except Exception: pass
        raise RuntimeError(f"Failed to execute FFmpeg for audio extraction: {str(exec_err)}")

    if proc.returncode != 0:
        if out_wav.exists():
            try: os.remove(str(out_wav))
            except Exception: pass
        err_msg = proc.stderr.strip() if proc.stderr else f"Exit code {proc.returncode}"
        raise RuntimeError(f"FFmpeg audio extraction failed: {err_msg[:300]}")

    if not out_wav.is_file() or out_wav.stat().st_size <= 44:
        if out_wav.exists():
            try: os.remove(str(out_wav))
            except Exception: pass
        raise RuntimeError("FFmpeg audio extraction produced an empty or invalid audio file.")

    # Verify extracted WAV header
    try:
        with open(out_wav, "rb") as f:
            hdr = f.read(12)
        if len(hdr) < 12 or hdr[:4] != b"RIFF" or hdr[8:12] != b"WAVE":
            if out_wav.exists():
                try: os.remove(str(out_wav))
                except Exception: pass
            raise RuntimeError("Extracted audio is not a valid RIFF WAVE file.")
    except Exception as read_err:
        if out_wav.exists():
            try: os.remove(str(out_wav))
            except Exception: pass
        raise RuntimeError(f"Failed to read extracted audio file: {str(read_err)}")

    return str(out_wav.resolve())


def prepare_reference_audio(file_path):
    """
    Inspects, validates, and prepares the reference KYC audio file for Detectif.ai Eidos enrollment.
    Detectif.ai documents WAV, MP3, FLAC, OGG/Opus, WebM, M4A/AAC, and AIFF. It does not document MP4.
    If the file is an MP4 video container, extracts its audio stream into a temporary 16kHz WAV file.
    Enforces that the file contains usable audio and that its duration falls within 15-60 seconds.

    Returns:
        tuple (prepared_file_path: str, mime_type: str, is_temporary: bool)
    Raises:
        ValueError / RuntimeError if file is missing, empty, lacks audio, format unsupported,
        extraction fails, or duration requirement (15-60s) is violated.
    """
    path_obj = Path(file_path)
    if not path_obj.is_file():
        raise ValueError(f"Reference audio file not found: {file_path}")

    size = path_obj.stat().st_size
    if size == 0:
        raise ValueError(f"Reference audio file is empty (0 bytes): {file_path}")

    with open(path_obj, "rb") as f:
        magic = f.read(16)

    ext = path_obj.suffix.lower()

    # 1. MP4 container: DetectifAI does not accept MP4 video containers.
    # Extract audio stream into a temporary WAV file.
    is_mp4 = (ext == ".mp4") or (len(magic) >= 8 and magic[4:8] == b"ftyp" and ext != ".m4a")
    if is_mp4:
        with open(path_obj, "rb") as f:
            data = f.read(min(size, 2 * 1024 * 1024))
        pre_dur = _get_mp4_duration(data)
        if pre_dur is not None:
            if pre_dur < 15.0 or pre_dur > 60.0:
                raise ValueError(
                    f"Reference audio duration ({pre_dur:.1f}s) is outside the required 15-60s range for Detectif.ai enrollment."
                )

        # Extract audio stream to temporary WAV
        wav_path = extract_audio_from_mp4(str(path_obj))

        # Validate extracted WAV duration
        dur = _get_wav_duration(wav_path)
        if dur is not None:
            if dur < 15.0 or dur > 60.0:
                try: os.remove(wav_path)
                except Exception: pass
                raise ValueError(
                    f"Extracted reference audio duration ({dur:.1f}s) is outside the required 15-60s range for Detectif.ai enrollment."
                )

        return wav_path, "audio/wav", True

    # 2. RIFF WAV
    if (len(magic) >= 12 and magic[:4] == b"RIFF" and magic[8:12] == b"WAVE") or ext == ".wav":
        dur = _get_wav_duration(path_obj)
        if dur is not None:
            if dur < 15.0 or dur > 60.0:
                raise ValueError(
                    f"Reference audio duration ({dur:.1f}s) is outside the required 15-60s range for Detectif.ai enrollment."
                )
        return str(path_obj.resolve()), "audio/wav", False

    # 3. M4A
    if ext == ".m4a" or (len(magic) >= 8 and magic[4:8] == b"ftyp" and ext == ".m4a"):
        with open(path_obj, "rb") as f:
            data = f.read(min(size, 2 * 1024 * 1024))
        dur = _get_mp4_duration(data)
        if dur is not None:
            if dur < 15.0 or dur > 60.0:
                raise ValueError(
                    f"Reference audio duration ({dur:.1f}s) is outside the required 15-60s range for Detectif.ai enrollment."
                )
        return str(path_obj.resolve()), "audio/mp4", False

    # 4. Other documented formats
    detected_mime, _ = detect_audio_mime_and_ext(str(path_obj))
    if detected_mime in ("audio/mpeg", "audio/ogg", "audio/flac", "audio/webm", "audio/aac", "audio/aiff"):
        return str(path_obj.resolve()), detected_mime, False

    # 5. Unsupported format
    raise ValueError(
        f"Unsupported reference audio format '{ext}'. Detectif.ai documents WAV, MP3, FLAC, OGG, WebM, M4A, and AIFF."
    )


class DetectifAIVerifier:
    """
    Strict, fail-closed client for Detectif.ai Aletheia and Eidos REST APIs.
    """
    def __init__(self, api_key=None, api_url=None, supabase_gateway=None, mode="cloud"):
        self.mode = mode or "cloud"
        # When api_key is explicitly provided (including empty string), respect it;
        # only inspect environment variables when api_key is None.
        if api_key is not None:
            self.api_key = str(api_key).strip()
        else:
            self.api_key = (
                os.getenv("DETECTIF_API_KEY")
                or os.getenv("DETECTIFAI_API_KEY")
                or os.getenv("DETECTIF_AI_KEY")
                or ""
            ).strip()

        if api_url is not None:
            raw_url = str(api_url).strip()
        else:
            raw_url = (
                os.getenv("DETECTIF_API_URL")
                or os.getenv("DETECTIFAI_API_URL")
                or "https://platform.detectif.ai/v1"
            )
        self.api_url = raw_url.rstrip("/")
        self.supabase_gateway = supabase_gateway or SupabaseAudioGateway()

    def is_api_configured(self):
        """Returns True if a valid API key string is present."""
        return bool(self.api_key and len(self.api_key) > 5)

    def _get_headers(self, idempotent=False):
        """Builds standard headers with Bearer authentication and optional Idempotency-Key."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json"
        }
        if idempotent:
            headers["Idempotency-Key"] = str(uuid.uuid4())
        return headers

    def _parse_api_error(self, resp):
        """
        Extracts structured error information according to Detectif.ai error schema:
        { "error": { "type", "code", "message", "param" }, "request_id" }
        """
        request_id = resp.headers.get("X-Request-Id")
        try:
            body = resp.json()
            if isinstance(body, dict):
                request_id = body.get("request_id") or request_id
                err = body.get("error")
                if isinstance(err, dict):
                    return {
                        "error_code": err.get("code") or f"http_{resp.status_code}",
                        "error_type": err.get("type") or "api_error",
                        "error_message": err.get("message") or f"HTTP {resp.status_code}",
                        "error_param": err.get("param"),
                        "request_id": request_id,
                        "status_code": resp.status_code
                    }
        except Exception:
            pass

        return {
            "error_code": f"http_{resp.status_code}",
            "error_type": "http_error",
            "error_message": resp.text[:300] or f"HTTP {resp.status_code}",
            "error_param": None,
            "request_id": request_id,
            "status_code": resp.status_code
        }

    # ── Official Route 1: Aletheia Deepfake Detection ──────────────────────────
    def detect_deepfake(self, audio_path, mime_type=None):
        """
        Official Detectif.ai Aletheia Endpoint:
        POST /v1/deepfake/detect

        Contract:
        - Multipart field: 'file' (up to 100 MB)
        - Auth: Bearer <DETECTIF_API_KEY>
        - Decision: result.verdict in ('real', 'fake', 'not_sure')
        - Fail-closed: Never overrides verdict with local threshold, never reports 'not_sure' as pass.
        """
        if self.mode == "local_debug":
            return self._local_audio_deepfake_analysis(audio_path)

        if not self.is_api_configured():
            return {
                "is_cloud": False,
                "status": "error",
                "verdict": "error",
                "is_deepfake": False,
                "error_code": "missing_api_key",
                "error": "Detectif.ai API key is missing. Configure DETECTIF_API_KEY in your environment.",
                "model": "Detectif.ai Aletheia",
                "flag_reasons": ["Detectif.ai Aletheia check failed: missing API key."]
            }

        if not os.path.isfile(audio_path):
            return {
                "is_cloud": False,
                "status": "error",
                "verdict": "error",
                "is_deepfake": False,
                "error_code": "file_not_found",
                "error": f"Audio file not found: {audio_path}",
                "model": "Detectif.ai Aletheia",
                "flag_reasons": [f"Audio file not found: {audio_path}"]
            }

        detected_mime, _ = detect_audio_mime_and_ext(audio_path, mime_type)
        if detected_mime.startswith("video/"):
            return {
                "is_cloud": True,
                "status": "error",
                "verdict": "error",
                "is_deepfake": False,
                "error_code": "unsupported_media_type",
                "error": f"Detectif.ai Aletheia does not support video containers ({detected_mime}). Audio must be extracted to a supported audio format.",
                "model": "Detectif.ai Aletheia",
                "flag_reasons": ["Detectif.ai Aletheia does not support video containers."]
            }

        url = f"{self.api_url}/deepfake/detect"
        headers = self._get_headers(idempotent=True)

        try:
            with open(audio_path, "rb") as f:
                files = {"file": (Path(audio_path).name, f, detected_mime)}
                resp = requests.post(url, headers=headers, files=files, timeout=30)
        except requests.exceptions.Timeout as t_err:
            return {
                "is_cloud": True,
                "status": "error",
                "verdict": "error",
                "is_deepfake": False,
                "error_code": "timeout",
                "error": f"Detectif.ai Aletheia request timed out: {str(t_err)}",
                "model": "Detectif.ai Aletheia",
                "flag_reasons": ["Detectif.ai Aletheia request timed out."]
            }
        except Exception as net_err:
            return {
                "is_cloud": True,
                "status": "error",
                "verdict": "error",
                "is_deepfake": False,
                "error_code": "network_error",
                "error": f"Detectif.ai Aletheia connection failed: {str(net_err)}",
                "model": "Detectif.ai Aletheia",
                "flag_reasons": [f"Detectif.ai Aletheia network error: {str(net_err)}"]
            }

        if resp.status_code != 200:
            err_info = self._parse_api_error(resp)
            return {
                "is_cloud": True,
                "status": "error",
                "verdict": "error",
                "is_deepfake": False,
                "error_code": err_info["error_code"],
                "error_type": err_info["error_type"],
                "error": err_info["error_message"],
                "request_id": err_info["request_id"],
                "http_status": err_info["status_code"],
                "model": "Detectif.ai Aletheia",
                "flag_reasons": [f"Detectif.ai Aletheia error ({err_info['error_code']}): {err_info['error_message']}"]
            }

        # Parse and validate JSON schema strictly
        try:
            data = resp.json()
        except Exception as json_err:
            return {
                "is_cloud": True,
                "status": "error",
                "verdict": "error",
                "is_deepfake": False,
                "error_code": "invalid_json",
                "error": f"Detectif.ai Aletheia returned invalid JSON: {str(json_err)}",
                "request_id": resp.headers.get("X-Request-Id"),
                "model": "Detectif.ai Aletheia",
                "flag_reasons": ["Detectif.ai Aletheia returned unparseable JSON."]
            }

        validation_err = self._validate_aletheia_schema(data)
        if validation_err:
            return {
                "is_cloud": True,
                "status": "error",
                "verdict": "error",
                "is_deepfake": False,
                "error_code": "malformed_response",
                "error": f"Detectif.ai Aletheia malformed payload: {validation_err}",
                "request_id": data.get("request_id") if isinstance(data, dict) else None,
                "model": "Detectif.ai Aletheia",
                "flag_reasons": [f"Detectif.ai Aletheia malformed response: {validation_err}"]
            }

        res = data["result"]
        verdict = res["verdict"]  # 'real', 'fake', or 'not_sure'
        score = float(res["score"])
        threshold = float(res["threshold"])
        is_synthetic = bool(res["is_synthetic"])

        flag_reasons = []
        if verdict == "fake":
            status = "flagged"
            is_deepfake = True
            flag_reasons.append(
                f"Detectif.ai Aletheia detected AI synthetic speech (Verdict: FAKE, Score: {score:.4f}, Threshold: {threshold})"
            )
        elif verdict == "real":
            status = "verified"
            is_deepfake = False
        elif verdict == "not_sure":
            status = "inconclusive"
            is_deepfake = False
            flag_reasons.append("Detectif.ai Aletheia verdict was 'not_sure' (inconclusive / low speech clarity).")
        else:
            status = "error"
            is_deepfake = False
            flag_reasons.append(f"Unknown Aletheia verdict '{verdict}'.")

        return {
            "is_cloud": True,
            "status": status,
            "verdict": verdict,
            "label": res.get("label"),
            "is_synthetic": is_synthetic,
            "is_deepfake": is_deepfake,
            "score": round(score, 4),
            "threshold": threshold,
            "model": f"Detectif.ai Aletheia ({data.get('model', {}).get('name', 'aletheia')})",
            "operation_id": data.get("operation_id"),
            "request_id": data.get("request_id"),
            "duration_seconds": res.get("duration_seconds"),
            "details": res,
            "flag_reasons": flag_reasons
        }

    def _validate_aletheia_schema(self, data):
        """Validates that Aletheia response meets OpenAPI contract without contradictions."""
        if not isinstance(data, dict):
            return "Root response must be a JSON object."
        if not data.get("request_id"):
            return "Missing required field 'request_id'."
        res = data.get("result")
        if not isinstance(res, dict):
            return "Missing required object 'result'."
        verdict = res.get("verdict")
        if verdict not in ("real", "fake", "not_sure"):
            return f"result.verdict must be one of ('real', 'fake', 'not_sure'), got '{verdict}'."
        if "score" not in res or not isinstance(res["score"], (int, float)):
            return "result.score must be a number."
        if not (0.0 <= float(res["score"]) <= 1.0):
            return f"result.score must be between 0 and 1, got {res['score']}."
        if "threshold" not in res or not isinstance(res["threshold"], (int, float)):
            return "result.threshold must be a number."
        if "is_synthetic" not in res or not isinstance(res["is_synthetic"], bool):
            return "result.is_synthetic must be a boolean."

        # Contradiction check:
        if verdict == "fake" and not res["is_synthetic"]:
            return "Contradiction: verdict is 'fake' but is_synthetic is false."
        if verdict == "real" and res["is_synthetic"]:
            return "Contradiction: verdict is 'real' but is_synthetic is true."

        return None

    # ── Official Route 2: Eidos Speaker Management & Verification ─────────────
    def get_speaker(self, speaker_id):
        """
        Official Detectif.ai Eidos Endpoint:
        GET /v1/speakers/{speaker_id}
        """
        try:
            clean_id = validate_speaker_id(speaker_id)
        except ValueError as err:
            return {"success": False, "error_code": "invalid_speaker_id", "error": str(err)}

        if not self.is_api_configured():
            return {"success": False, "error_code": "missing_api_key", "error": "Detectif.ai API key not configured"}

        url = f"{self.api_url}/speakers/{clean_id}"
        try:
            resp = requests.get(url, headers=self._get_headers(), timeout=10)
        except Exception as e:
            return {"success": False, "error_code": "network_error", "error": str(e)}

        if resp.status_code == 200:
            try:
                data = resp.json()
                return {"success": True, "speaker": data, "request_id": data.get("request_id")}
            except Exception as json_err:
                return {"success": False, "error_code": "invalid_json", "error": str(json_err)}
        elif resp.status_code == 404:
            err_info = self._parse_api_error(resp)
            return {
                "success": False,
                "not_found": True,
                "error_code": "speaker_not_found",
                "request_id": err_info["request_id"]
            }
        else:
            err_info = self._parse_api_error(resp)
            return {
                "success": False,
                "error_code": err_info["error_code"],
                "error": err_info["error_message"],
                "request_id": err_info["request_id"],
                "status_code": resp.status_code
            }

    def delete_speaker(self, speaker_id):
        """
        Official Detectif.ai Eidos Endpoint:
        DELETE /v1/speakers/{speaker_id}
        """
        try:
            clean_id = validate_speaker_id(speaker_id)
        except ValueError as err:
            return {"success": False, "error_code": "invalid_speaker_id", "error": str(err)}

        if not self.is_api_configured():
            return {"success": False, "error_code": "missing_api_key", "error": "Detectif.ai API key not configured"}

        url = f"{self.api_url}/speakers/{clean_id}"
        try:
            resp = requests.delete(url, headers=self._get_headers(), timeout=30)
        except Exception as e:
            return {"success": False, "error_code": "network_error", "error": str(e)}

        if resp.status_code in (200, 202, 204, 404):
            return {"success": True}

        err_info = self._parse_api_error(resp)
        return {
            "success": False,
            "error_code": err_info["error_code"],
            "error": err_info["error_message"],
            "request_id": err_info["request_id"],
            "status_code": resp.status_code
        }

    def _enrolled_fingerprint_path(self, speaker_id):
        return Path(self.supabase_gateway.cache_dir) / f"enrolled_{speaker_id}.sha256"

    @staticmethod
    def _file_sha256(file_path):
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def enroll_speaker(self, audio_path, speaker_id, display_name=None, mime_type=None):
        """
        Official Detectif.ai Eidos Endpoint:
        POST /v1/speakers/enroll

        Contract:
        - Multipart field: 'file' (up to 100 MB)
        - Form field: 'speaker_id' (1-64 chars: ^[A-Za-z0-9_.:-]{1,64}$)
        - Optional form field: 'display_name'
        - Auth: Bearer <DETECTIF_API_KEY>
        """
        try:
            valid_speaker_id = validate_speaker_id(speaker_id)
        except ValueError as err:
            return {"success": False, "error_code": "invalid_speaker_id", "error": str(err)}

        if not self.is_api_configured():
            return {"success": False, "error_code": "missing_api_key", "error": "Detectif.ai API key not configured"}

        if not os.path.isfile(audio_path):
            return {"success": False, "error_code": "file_not_found", "error": f"Audio file not found: {audio_path}"}

        detected_mime, _ = detect_audio_mime_and_ext(audio_path, mime_type)
        if detected_mime.startswith("video/"):
            return {
                "success": False,
                "error_code": "unsupported_media_type",
                "error": f"Detectif.ai Eidos does not support video containers ({detected_mime}). Audio stream must be extracted to WAV or a supported audio container."
            }

        url = f"{self.api_url}/speakers/enroll"
        headers = self._get_headers(idempotent=True)

        try:
            with open(audio_path, "rb") as f:
                files = {"file": (Path(audio_path).name, f, detected_mime)}
                data = {"speaker_id": valid_speaker_id}
                if display_name:
                    data["display_name"] = str(display_name)[:128]

                resp = requests.post(url, headers=headers, files=files, data=data, timeout=300)
        except requests.exceptions.Timeout as t_err:
            return {"success": False, "error_code": "timeout", "error": f"Detectif.ai Eidos enroll timed out: {str(t_err)}"}
        except Exception as e:
            return {"success": False, "error_code": "network_error", "error": str(e)}

        if resp.status_code in (200, 201):
            try:
                body = resp.json()
                if not isinstance(body, dict) or "speaker" not in body:
                    return {"success": False, "error_code": "malformed_response", "error": "Missing 'speaker' in enrollment response."}
                return {"success": True, "data": body, "request_id": body.get("request_id")}
            except Exception as json_err:
                return {"success": False, "error_code": "invalid_json", "error": str(json_err)}

        err_info = self._parse_api_error(resp)
        return {
            "success": False,
            "error_code": err_info["error_code"],
            "error": err_info["error_message"],
            "request_id": err_info["request_id"],
            "status_code": resp.status_code
        }

    def _post_speaker_verify(self, audio_path, speaker_id, mime_type=None):
        """Internal helper executing POST /v1/speakers/verify."""
        detected_mime, _ = detect_audio_mime_and_ext(audio_path, mime_type)
        if detected_mime.startswith("video/"):
            return {
                "is_cloud": True,
                "status": "error",
                "decision": "error",
                "is_match": False,
                "error_code": "unsupported_media_type",
                "error": f"Detectif.ai Eidos verify does not accept video containers ({detected_mime}). Session audio must be audio/webm, audio/ogg, or audio/wav.",
                "candidate_id": speaker_id,
                "model": "Detectif.ai Eidos",
                "flag_reasons": ["Detectif.ai Eidos verify does not accept video containers."]
            }

        url = f"{self.api_url}/speakers/verify"
        headers = self._get_headers(idempotent=True)

        try:
            with open(audio_path, "rb") as f:
                files = {"file": (Path(audio_path).name, f, detected_mime)}
                data = {"speaker_id": speaker_id}
                resp = requests.post(url, headers=headers, files=files, data=data, timeout=300)
        except requests.exceptions.Timeout as t_err:
            return {
                "is_cloud": True,
                "status": "error",
                "decision": "error",
                "is_match": False,
                "error_code": "timeout",
                "error": f"Detectif.ai Eidos verify timed out: {str(t_err)}",
                "candidate_id": speaker_id,
                "model": "Detectif.ai Eidos",
                "flag_reasons": ["Detectif.ai Eidos request timed out."]
            }
        except Exception as net_err:
            return {
                "is_cloud": True,
                "status": "error",
                "decision": "error",
                "is_match": False,
                "error_code": "network_error",
                "error": f"Detectif.ai Eidos verify connection failed: {str(net_err)}",
                "candidate_id": speaker_id,
                "model": "Detectif.ai Eidos",
                "flag_reasons": [f"Detectif.ai Eidos network error: {str(net_err)}"]
            }

        if resp.status_code != 200:
            err_info = self._parse_api_error(resp)
            return {
                "is_cloud": True,
                "status": "error",
                "decision": "error",
                "is_match": False,
                "error_code": err_info["error_code"],
                "error_type": err_info["error_type"],
                "error": err_info["error_message"],
                "request_id": err_info["request_id"],
                "http_status": err_info["status_code"],
                "candidate_id": speaker_id,
                "model": "Detectif.ai Eidos",
                "flag_reasons": [f"Detectif.ai Eidos error ({err_info['error_code']}): {err_info['error_message']}"]
            }

        try:
            api_data = resp.json()
        except Exception as json_err:
            return {
                "is_cloud": True,
                "status": "error",
                "decision": "error",
                "is_match": False,
                "error_code": "invalid_json",
                "error": f"Detectif.ai Eidos returned invalid JSON: {str(json_err)}",
                "candidate_id": speaker_id,
                "model": "Detectif.ai Eidos",
                "flag_reasons": ["Detectif.ai Eidos returned unparseable JSON."]
            }

        validation_err = self._validate_eidos_schema(api_data)
        if validation_err:
            return {
                "is_cloud": True,
                "status": "error",
                "decision": "error",
                "is_match": False,
                "error_code": "malformed_response",
                "error": f"Detectif.ai Eidos malformed payload: {validation_err}",
                "request_id": api_data.get("request_id") if isinstance(api_data, dict) else None,
                "candidate_id": speaker_id,
                "model": "Detectif.ai Eidos",
                "flag_reasons": [f"Detectif.ai Eidos malformed response: {validation_err}"]
            }

        res = api_data["result"]
        decision = res["decision"]  # 'match' or 'no_match'
        similarity = float(res["similarity"])
        threshold = float(res["threshold"])

        flag_reasons = []
        if decision == "match":
            status = "verified"
            is_match = True
        elif decision == "no_match":
            status = "flagged"
            is_match = False
            flag_reasons.append(
                f"Detectif.ai Eidos detected speaker mismatch (Decision: NO_MATCH, Similarity: {similarity:+.4f}, Threshold: {threshold})"
            )
        else:
            status = "error"
            is_match = False
            flag_reasons.append(f"Unknown Eidos decision '{decision}'.")

        return {
            "is_cloud": True,
            "status": status,
            "decision": decision,
            "is_match": is_match,
            "similarity": similarity,
            "threshold": threshold,
            "similarity_formatted": f"{similarity:+.4f}",
            "candidate_id": speaker_id,
            "speaker_id": api_data.get("speaker_id") or speaker_id,
            "model": f"Detectif.ai Eidos ({api_data.get('model', {}).get('name', 'eidos')})",
            "operation_id": api_data.get("operation_id"),
            "request_id": api_data.get("request_id"),
            "duration_seconds": res.get("duration_seconds"),
            "details": res,
            "flag_reasons": flag_reasons
        }

    def _validate_eidos_schema(self, data):
        """Validates that Eidos verify response strictly satisfies OpenAPI contract."""
        if not isinstance(data, dict):
            return "Root response must be a JSON object."
        if not data.get("request_id"):
            return "Missing required field 'request_id'."
        res = data.get("result")
        if not isinstance(res, dict):
            return "Missing required object 'result'."
        decision = res.get("decision")
        if decision not in ("match", "no_match"):
            return f"result.decision must be 'match' or 'no_match', got '{decision}'."
        if "similarity" not in res or not isinstance(res["similarity"], (int, float)):
            return "result.similarity must be a number."
        sim = float(res["similarity"])
        if not (-1.0 <= sim <= 1.0):
            return f"result.similarity must be between -1.0 and 1.0, got {sim}."
        if "threshold" not in res or not isinstance(res["threshold"], (int, float)):
            return "result.threshold must be a number."
        return None

    def verify_speaker(self, test_audio_path, candidate_id="candidate-001", local_reference_path=None, mime_type=None):
        """
        Official Detectif.ai Eidos Speaker Verification Flow:
        1. Validates candidate_id.
        2. In cloud mode: verifies ONLY after confirming speaker exists or enrolling reference sample.
           If lookup or enrollment fails, STOPS and reports the failure.
        3. Calls POST /v1/speakers/verify with session audio.
        4. If local_debug mode is requested, runs strictly labeled local analysis.
        """
        # Validate candidate_id strictly
        try:
            valid_speaker_id = validate_speaker_id(candidate_id)
        except ValueError as val_err:
            return {
                "is_cloud": False,
                "status": "error",
                "decision": "error",
                "is_match": False,
                "error_code": "invalid_speaker_id",
                "error": str(val_err),
                "candidate_id": candidate_id,
                "model": "Detectif.ai Eidos",
                "flag_reasons": [str(val_err)]
            }

        if self.mode == "local_debug":
            fetch_res = self.supabase_gateway.fetch_reference_audio(valid_speaker_id, local_override_path=local_reference_path)
            if not fetch_res.get("success"):
                return {
                    "is_cloud": False,
                    "status": "error",
                    "decision": "error",
                    "is_match": False,
                    "error_code": "reference_audio_missing",
                    "error": f"Reference audio not found for candidate '{valid_speaker_id}': {fetch_res.get('error')}",
                    "candidate_id": valid_speaker_id,
                    "model": "Local Voice Biometrics (Debug Only)",
                    "flag_reasons": [f"Reference audio missing for '{valid_speaker_id}'."]
                }
            local_res = self._local_speaker_verification(test_audio_path, fetch_res["file_path"], valid_speaker_id)
            local_res["reference_source"] = fetch_res["source"]
            return local_res

        if not self.is_api_configured():
            return {
                "is_cloud": False,
                "status": "error",
                "decision": "error",
                "is_match": False,
                "error_code": "missing_api_key",
                "error": "Detectif.ai API key is missing. Set DETECTIF_API_KEY in your environment.",
                "candidate_id": valid_speaker_id,
                "model": "Detectif.ai Eidos",
                "flag_reasons": ["Detectif.ai Eidos check failed: missing API key."]
            }

        if not os.path.isfile(test_audio_path):
            return {
                "is_cloud": False,
                "status": "error",
                "decision": "error",
                "is_match": False,
                "error_code": "file_not_found",
                "error": f"Test audio file not found: {test_audio_path}",
                "candidate_id": valid_speaker_id,
                "model": "Detectif.ai Eidos",
                "flag_reasons": [f"Test audio file not found: {test_audio_path}"]
            }

        # Step 1: Check if speaker exists in Detectif.ai Eidos
        lookup = self.get_speaker(valid_speaker_id)

        # If lookup failed with an error OTHER than not_found (e.g. 401, 403, 500, network error):
        if not lookup.get("success") and not lookup.get("not_found"):
            return {
                "is_cloud": True,
                "status": "error",
                "decision": "error",
                "is_match": False,
                "error_code": lookup.get("error_code") or "speaker_lookup_failed",
                "error": f"Speaker existence check failed: {lookup.get('error')}",
                "request_id": lookup.get("request_id"),
                "http_status": lookup.get("status_code"),
                "candidate_id": valid_speaker_id,
                "model": "Detectif.ai Eidos",
                "flag_reasons": [f"Detectif.ai Eidos speaker lookup failed: {lookup.get('error')}"]
            }

        ref_source = "enrolled_cloud"
        temp_files_to_clean = []
        try:
            # An explicitly supplied reference file (e.g. Test_Audio.m4a) must be the real voice.
            # If the speaker already exists but was not enrolled from this exact file, replace it.
            need_enroll = bool(lookup.get("not_found"))
            ref_fingerprint = None
            if not need_enroll and local_reference_path and os.path.isfile(local_reference_path):
                ref_fingerprint = self._file_sha256(local_reference_path)
                fp_path = self._enrolled_fingerprint_path(valid_speaker_id)
                stored_fp = fp_path.read_text().strip() if fp_path.is_file() else None
                if stored_fp != ref_fingerprint:
                    del_res = self.delete_speaker(valid_speaker_id)
                    if not del_res.get("success"):
                        return {
                            "is_cloud": True,
                            "status": "error",
                            "decision": "error",
                            "is_match": False,
                            "error_code": del_res.get("error_code") or "speaker_reset_failed",
                            "error": f"Could not reset existing speaker voiceprint: {del_res.get('error')}",
                            "request_id": del_res.get("request_id"),
                            "http_status": del_res.get("status_code"),
                            "candidate_id": valid_speaker_id,
                            "model": "Detectif.ai Eidos",
                            "flag_reasons": [f"Detectif.ai Eidos speaker reset failed: {del_res.get('error')}"]
                        }
                    need_enroll = True

            # Step 2: If speaker is not found (404) or must be re-enrolled, enroll using the reference sample
            if need_enroll:
                fetch_res = self.supabase_gateway.fetch_reference_audio(valid_speaker_id, local_override_path=local_reference_path)
                if not fetch_res.get("success"):
                    return {
                        "is_cloud": True,
                        "status": "error",
                        "decision": "error",
                        "is_match": False,
                        "error_code": "reference_audio_missing",
                        "error": f"Speaker '{valid_speaker_id}' is not enrolled and reference KYC sample could not be fetched: {fetch_res.get('error')}",
                        "candidate_id": valid_speaker_id,
                        "model": "Detectif.ai Eidos",
                        "flag_reasons": [f"Reference audio missing for '{valid_speaker_id}': {fetch_res.get('error')}"]
                    }

                ref_audio_path = fetch_res["file_path"]
                ref_source = fetch_res["source"]

                try:
                    prep_res = prepare_reference_audio(ref_audio_path)
                    prepared_ref_path = prep_res[0]
                    prepared_mime = prep_res[1]
                    is_temp = prep_res[2] if len(prep_res) > 2 else False
                    if is_temp:
                        temp_files_to_clean.append(prepared_ref_path)
                except Exception as prep_err:
                    return {
                        "is_cloud": True,
                        "status": "error",
                        "decision": "error",
                        "is_match": False,
                        "error_code": "reference_audio_invalid",
                        "error": f"Reference audio preparation failed: {str(prep_err)}",
                        "candidate_id": valid_speaker_id,
                        "reference_source": ref_source,
                        "model": "Detectif.ai Eidos",
                        "flag_reasons": [f"Reference audio invalid for '{valid_speaker_id}': {str(prep_err)}"]
                    }

                enroll_res = self.enroll_speaker(
                    audio_path=prepared_ref_path,
                    speaker_id=valid_speaker_id,
                    display_name=f"Candidate {valid_speaker_id}",
                    mime_type=prepared_mime
                )

                # If enrollment fails, STOP AND REPORT THE FAILURE!
                if not enroll_res.get("success"):
                    return {
                        "is_cloud": True,
                        "status": "error",
                        "decision": "error",
                        "is_match": False,
                        "error_code": enroll_res.get("error_code") or "enrollment_failed",
                        "error": f"Candidate voice enrollment failed: {enroll_res.get('error')}",
                        "request_id": enroll_res.get("request_id"),
                        "http_status": enroll_res.get("status_code"),
                        "candidate_id": valid_speaker_id,
                        "reference_source": ref_source,
                        "model": "Detectif.ai Eidos",
                        "flag_reasons": [f"Detectif.ai Eidos enrollment failed: {enroll_res.get('error')}"]
                    }

                try:
                    if local_reference_path and os.path.isfile(local_reference_path):
                        self._enrolled_fingerprint_path(valid_speaker_id).write_text(
                            ref_fingerprint or self._file_sha256(local_reference_path)
                        )
                except Exception:
                    pass

            # Step 3: Speaker is confirmed to exist. Perform verification
            verify_result = self._post_speaker_verify(test_audio_path, valid_speaker_id, mime_type=mime_type)
            verify_result["reference_source"] = ref_source
            return verify_result
        finally:
            for p in temp_files_to_clean:
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                except Exception:
                    pass

    # ── Local Fallback biometrics (Labeled strictly as Local Debug Only) ───────
    def _read_wav_data(self, wav_path):
        """
        Reads genuine linear PCM WAV files only.
        Raises ValueError for invalid, corrupted, or non-RIFF container formats (e.g. WebM/OGG).
        Never treats compressed container bytes as PCM.
        """
        if not os.path.isfile(wav_path):
            raise FileNotFoundError(f"Audio file not found: {wav_path}")

        with open(wav_path, "rb") as f:
            header = f.read(12)
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise ValueError(
                f"Unsupported format for local WAV analysis: '{Path(wav_path).name}' is not a valid RIFF WAV file. "
                "Compressed containers (WebM, OGG, MP3) must be decoded before local PCM analysis."
            )

        sr, data = wavfile.read(wav_path)
        if data.ndim > 1:
            data = data.mean(axis=1)
        if data.dtype == np.int16:
            data = data.astype(np.float32) / 32768.0
        elif data.dtype == np.int32:
            data = data.astype(np.float32) / 2147483648.0
        elif data.dtype == np.uint8:
            data = (data.astype(np.float32) - 128.0) / 128.0
        elif data.dtype in (np.float32, np.float64):
            data = data.astype(np.float32)
        else:
            raise ValueError(f"Unsupported sample format: {data.dtype}")
        return sr, data

    def _local_audio_deepfake_analysis(self, audio_path):
        """
        Local-only spectral analysis for debugging.
        Explicitly labeled as local-only; never reported as Detectif.ai or verified pass.
        """
        try:
            sr, data = self._read_wav_data(audio_path)
        except Exception as dec_err:
            return {
                "is_cloud": False,
                "status": "error",
                "verdict": "error",
                "is_deepfake": False,
                "error_code": "local_decoder_error",
                "error": str(dec_err),
                "model": "Local Debug Only (Not DetectifAI)",
                "flag_reasons": [f"Local decoder error: {str(dec_err)}"]
            }

        try:
            if len(data) < 800:
                return {
                    "is_cloud": False,
                    "status": "local_debug_only",
                    "verdict": "inconclusive",
                    "is_deepfake": False,
                    "score": 0.05,
                    "model": "Local Debug Only (Not DetectifAI)",
                    "note": "Debug audio too short for spectral analysis.",
                    "flag_reasons": []
                }

            frequencies, times, spec = scipy.signal.spectrogram(data, fs=sr, nperseg=512)
            spec_mag = np.abs(spec) + 1e-10

            hf_mask = frequencies > 3500
            total_energy = np.sum(spec_mag)
            hf_energy = np.sum(spec_mag[hf_mask, :]) if np.any(hf_mask) else 0.0
            hf_ratio = float(hf_energy / (total_energy + 1e-10))

            temporal_spec_diff = np.mean(np.abs(np.diff(spec_mag, axis=1)))
            synthetic_score = float(np.clip(hf_ratio * 1.8 + (0.15 if temporal_spec_diff < 0.01 else 0.0), 0.0, 1.0))
            is_deepfake = synthetic_score >= 0.55

            flag_reasons = []
            if is_deepfake:
                flag_reasons.append(f"[DEBUG ONLY] Local acoustic heuristic flagged anomaly (Score: {synthetic_score:.2f})")

            return {
                "is_cloud": False,
                "status": "local_debug_only",
                "verdict": "fake" if is_deepfake else "real",
                "is_deepfake": is_deepfake,
                "score": round(synthetic_score, 4),
                "model": "Local Debug Heuristic (Not DetectifAI)",
                "note": "Local-only debug analysis. Does not constitute Detectif.ai verification.",
                "flag_reasons": flag_reasons
            }
        except Exception as e:
            return {
                "is_cloud": False,
                "status": "error",
                "verdict": "error",
                "is_deepfake": False,
                "error_code": "local_analysis_failed",
                "error": str(e),
                "model": "Local Debug Only (Not DetectifAI)",
                "flag_reasons": [f"Local analysis failed: {str(e)}"]
            }

    def _local_speaker_verification(self, test_audio_path, ref_audio_path, candidate_id):
        """
        Local-only spectral cosine matching for debugging.
        Explicitly labeled as local-only; never reported as Detectif.ai or verified pass.
        """
        try:
            sr1, data1 = self._read_wav_data(test_audio_path)
            sr2, data2 = self._read_wav_data(ref_audio_path)
        except Exception as dec_err:
            return {
                "is_cloud": False,
                "status": "error",
                "decision": "error",
                "is_match": False,
                "error_code": "local_decoder_error",
                "error": str(dec_err),
                "candidate_id": candidate_id,
                "model": "Local Debug Only (Not DetectifAI)",
                "flag_reasons": [f"Local decoder error: {str(dec_err)}"]
            }

        try:
            if len(data1) < 800 or len(data2) < 800:
                return {
                    "is_cloud": False,
                    "status": "local_debug_only",
                    "decision": "inconclusive",
                    "is_match": False,
                    "similarity": 0.0,
                    "similarity_formatted": "+0.0000",
                    "threshold": 0.60,
                    "model": "Local Debug Voiceprint (Not DetectifAI)",
                    "candidate_id": candidate_id,
                    "reference_file": Path(ref_audio_path).name,
                    "note": "Debug audio too short for spectral comparison.",
                    "flag_reasons": ["Debug audio too short for local spectral comparison."]
                }

            if sr1 != sr2 and sr2 > 0:
                num_samples = int(len(data2) * float(sr1) / sr2)
                data2 = scipy.signal.resample(data2, num_samples)
                sr2 = sr1

            n_fft = 1024
            f1, t1, s1 = scipy.signal.spectrogram(data1, fs=sr1, nperseg=n_fft)
            f2, t2, s2 = scipy.signal.spectrogram(data2, fs=sr2, nperseg=n_fft)

            voice_print1 = np.mean(s1, axis=1)
            voice_print2 = np.mean(s2, axis=1)

            norm1 = np.linalg.norm(voice_print1)
            norm2 = np.linalg.norm(voice_print2)

            if norm1 > 0 and norm2 > 0:
                cosine_sim = float(np.dot(voice_print1, voice_print2) / (norm1 * norm2))
            else:
                cosine_sim = 0.0

            similarity = float(np.clip(cosine_sim, -1.0, 1.0))
            is_match = similarity >= 0.60

            flag_reasons = []
            if not is_match:
                flag_reasons.append(f"[DEBUG ONLY] Local biometrics mismatch (Similarity: {similarity:+.4f})")

            return {
                "is_cloud": False,
                "status": "local_debug_only",
                "decision": "match" if is_match else "no_match",
                "is_match": is_match,
                "similarity": round(similarity, 4),
                "similarity_formatted": f"{similarity:+.4f}",
                "threshold": 0.60,
                "model": "Local Debug Voiceprint (Not DetectifAI)",
                "candidate_id": candidate_id,
                "reference_file": Path(ref_audio_path).name,
                "note": "Local-only debug analysis. Does not constitute Detectif.ai verification.",
                "flag_reasons": flag_reasons
            }
        except Exception as e:
            return {
                "is_cloud": False,
                "status": "error",
                "decision": "error",
                "is_match": False,
                "error_code": "local_biometrics_failed",
                "error": str(e),
                "candidate_id": candidate_id,
                "model": "Local Debug Only (Not DetectifAI)",
                "flag_reasons": [f"Local biometrics failed: {str(e)}"]
            }

    # ── Combined Pipeline ──────────────────────────────────────────────────────
    def verify_all(self, audio_path, candidate_id="candidate-001", local_reference_path=None, simulate_anomaly=None, mime_type=None):
        """
        Executes dual-check audio verification according to Detectif.ai contract:
        1. Aletheia: Deepfake detection (POST /v1/deepfake/detect)
        2. Eidos: Speaker verification (POST /v1/speakers/verify)

        Fail-closed rules:
        - cloud_verified is True ONLY when both checks return valid cloud 200 responses
          with verdict == 'real' and decision == 'match'.
        - If any check fails, errors, returns not_sure, or is local_debug, cloud_verified is False.
        """
        deepfake_res = self.detect_deepfake(audio_path, mime_type=mime_type)
        speaker_res = self.verify_speaker(audio_path, candidate_id=candidate_id, local_reference_path=local_reference_path, mime_type=mime_type)

        # Simulation options for debugging/testing flag UI
        if simulate_anomaly == "voice_deepfake":
            deepfake_res["status"] = "flagged"
            deepfake_res["verdict"] = "fake"
            deepfake_res["is_deepfake"] = True
            deepfake_res["score"] = 0.9127
            deepfake_res["flag_reasons"] = ["SIMULATED: Detectif.ai Aletheia detected synthetic voice cloning (Score: 0.9127)"]
        elif simulate_anomaly == "speaker_mismatch":
            speaker_res["status"] = "flagged"
            speaker_res["decision"] = "no_match"
            speaker_res["is_match"] = False
            speaker_res["similarity"] = -0.2314
            speaker_res["similarity_formatted"] = "-0.2314"
            speaker_res["flag_reasons"] = ["SIMULATED: Detectif.ai Eidos detected speaker mismatch (Similarity: -0.2314)"]

        all_flags = []
        if deepfake_res.get("flag_reasons"):
            all_flags.extend(deepfake_res["flag_reasons"])
        if speaker_res.get("flag_reasons"):
            all_flags.extend(speaker_res["flag_reasons"])

        has_audio_anomaly = bool(
            deepfake_res.get("status") == "flagged" or speaker_res.get("status") == "flagged"
        )
        has_inconclusive = bool(
            deepfake_res.get("status") == "inconclusive" or speaker_res.get("status") == "inconclusive"
        )
        has_error = bool(
            deepfake_res.get("status") == "error" or speaker_res.get("status") == "error"
        )

        # Strict fail-closed verification: True ONLY if both are genuine cloud checks and both verified
        cloud_verified = (
            deepfake_res.get("is_cloud") is True
            and deepfake_res.get("status") == "verified"
            and speaker_res.get("is_cloud") is True
            and speaker_res.get("status") == "verified"
            and not has_audio_anomaly
            and not has_inconclusive
            and not has_error
        )

        overall_status = "error" if has_error else ("flagged" if has_audio_anomaly else ("inconclusive" if has_inconclusive else ("verified" if cloud_verified else "indeterminate")))

        return {
            "success": True,
            "cloud_verified": cloud_verified,
            "overall_status": overall_status,
            "has_audio_anomaly": has_audio_anomaly,
            "has_inconclusive": has_inconclusive,
            "has_error": has_error,
            "candidate_id": candidate_id,
            "audio_deepfake": deepfake_res,
            "speaker_verification": speaker_res,
            "flag_reasons": all_flags,
            "summary": {
                "aletheia_verdict": deepfake_res.get("verdict"),
                "eidos_decision": speaker_res.get("decision"),
                "aletheia_cloud": deepfake_res.get("is_cloud", False),
                "eidos_cloud": speaker_res.get("is_cloud", False),
                "cloud_verified": cloud_verified
            }
        }


def main():
    parser = argparse.ArgumentParser(description="Strict Detectif.ai Audio Verification Engine")
    parser.add_argument("--audio", default="Test_Audio.mp4", help="Path to input audio file (default: Test_Audio.mp4)")
    parser.add_argument("--candidate-id", type=str, default="candidate-001", help="Candidate identifier")
    parser.add_argument("--reference-audio", type=str, default=None, help="Local override reference audio path")
    parser.add_argument("--simulate-anomaly", choices=["none", "voice_deepfake", "speaker_mismatch"], default=None, help="Simulate anomalies for UI testing")
    parser.add_argument("--mode", choices=["cloud", "local_debug"], default="cloud", help="Run mode (cloud by default)")
    parser.add_argument("--mime-type", type=str, default=None, help="Explicit audio MIME type")
    parser.add_argument("--supabase-url", type=str, default=None, help="Supabase project URL")
    parser.add_argument("--supabase-key", type=str, default=None, help="Supabase API key")
    parser.add_argument("--detectif-key", "--detectifai-key", dest="detectif_key", type=str, default=None, help="Detectif.ai API key")

    args = parser.parse_args()

    # Project root resolution for relative paths (e.g. Test_Audio.mp4)
    project_root = Path(__file__).resolve().parent.parent.parent
    raw_audio = args.audio or "Test_Audio.mp4"
    audio_path_obj = Path(raw_audio)
    if not audio_path_obj.is_absolute():
        if not audio_path_obj.exists():
            cand_path = project_root / raw_audio
            if cand_path.exists():
                audio_path_obj = cand_path
    audio_path = str(audio_path_obj.resolve())

    if not os.path.exists(audio_path):
        print(json.dumps({
            "success": False,
            "cloud_verified": False,
            "overall_status": "error",
            "has_error": True,
            "error_code": "file_not_found",
            "error": f"Audio file not found: {audio_path}"
        }))
        sys.exit(1)

    ref_audio = args.reference_audio
    if ref_audio:
        ref_path_obj = Path(ref_audio)
        if not ref_path_obj.is_absolute() and not ref_path_obj.exists():
            cand_ref = project_root / ref_audio
            if cand_ref.exists():
                ref_audio = str(cand_ref.resolve())

    try:
        gateway = SupabaseAudioGateway(
            supabase_url=args.supabase_url,
            supabase_key=args.supabase_key
        )
        verifier = DetectifAIVerifier(
            api_key=args.detectif_key,
            supabase_gateway=gateway,
            mode=args.mode
        )
        results = verifier.verify_all(
            audio_path=audio_path,
            candidate_id=args.candidate_id,
            local_reference_path=ref_audio,
            simulate_anomaly=args.simulate_anomaly,
            mime_type=args.mime_type
        )
        print(json.dumps(results))
    except Exception as e:
        print(json.dumps({
            "success": False,
            "cloud_verified": False,
            "overall_status": "error",
            "has_error": True,
            "error_code": "unhandled_exception",
            "error": f"Audio verification fatal error: {str(e)}",
            "traceback": traceback.format_exc()
        }))
        sys.exit(1)


if __name__ == "__main__":
    main()
