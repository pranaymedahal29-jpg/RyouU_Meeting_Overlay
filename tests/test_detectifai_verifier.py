"""
Unit tests for Detectif.ai Audio Verification Engine.
Strictly isolated: zero real API calls, mocked HTTP responses, zero reading of .env secrets.
"""

import math
import os
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch, MagicMock

import shutil
import subprocess

import requests

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src" / "main"))

from detectifai_verifier import (
    DetectifAIVerifier,
    validate_speaker_id,
    detect_audio_mime_and_ext,
    prepare_reference_audio,
    extract_audio_from_mp4,
    has_mp4_audio_stream
)


class MockResponse:
    def __init__(self, status_code, json_data, headers=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.headers = headers or {"X-Request-Id": "req_mock_default_id"}
        self.text = text or (str(json_data) if json_data else "")

    def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data


class TestDetectifAIVerifier(unittest.TestCase):

    def setUp(self):
        self.mock_api_key = "dtf_live_mock_test_key_abc123"
        self.mock_api_url = "https://platform.detectif.ai/v1"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)

        # Create dummy valid audio files for multipart tests
        # 1. Dummy WebM
        self.webm_file = self.dir_path / "test.webm"
        with open(self.webm_file, "wb") as f:
            f.write(b"\x1a\x45\xdf\xa3" + b"\x00" * 100)

        # 2. Dummy OGG
        self.ogg_file = self.dir_path / "test.ogg"
        with open(self.ogg_file, "wb") as f:
            f.write(b"OggS" + b"\x00" * 100)

        # 3. Dummy genuine linear PCM RIFF WAV (20.0s, 16000Hz, 16-bit mono sine wave)
        self.wav_file = self.dir_path / "test.wav"
        with wave.open(str(self.wav_file), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            raw_frames = bytearray()
            for i in range(16000):
                val = int(16000.0 * math.sin(2.0 * math.pi * 440.0 * i / 16000.0))
                raw_frames.extend(struct.pack("<h", val))
            wf.writeframes(raw_frames * 20)

        # 4. Project test audio (Test_Audio.mp4)
        self.test_audio_mp4 = PROJECT_ROOT / "Test_Audio.mp4"

        # Create mock gateway
        self.mock_gateway = MagicMock()
        def _fetch_ref(cid, local_override_path=None):
            return {
                "success": True,
                "file_path": local_override_path or str(self.wav_file),
                "source": "local_override" if local_override_path else "mock_supabase"
            }
        self.mock_gateway.fetch_reference_audio.side_effect = _fetch_ref

        self.verifier = DetectifAIVerifier(
            api_key=self.mock_api_key,
            api_url=self.mock_api_url,
            supabase_gateway=self.mock_gateway,
            mode="cloud"
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    # ── Test 1: Valid Aletheia & Eidos Cloud Responses ────────────────────────
    @patch("requests.post")
    @patch("requests.get")
    def test_valid_cloud_verification(self, mock_get, mock_post):
        # 1. GET /v1/speakers/candidate-001 -> speaker exists
        mock_get.return_value = MockResponse(200, {
            "request_id": "req_lookup_001",
            "speaker_id": "candidate-001",
            "samples": 1
        })

        # 2. POST /v1/deepfake/detect -> Aletheia real
        aletheia_body = {
            "request_id": "req_aletheia_001",
            "operation_id": "op_aletheia_001",
            "object": "deepfake.detection",
            "result": {
                "verdict": "real",
                "label": "genuine",
                "is_synthetic": False,
                "score": 0.0731,
                "threshold": 0.62,
                "duration_seconds": 6.4
            },
            "model": {"name": "aletheia", "version": "v1"}
        }

        # 3. POST /v1/speakers/verify -> Eidos match
        eidos_body = {
            "request_id": "req_eidos_001",
            "operation_id": "op_eidos_001",
            "object": "speaker.verification",
            "speaker_id": "candidate-001",
            "result": {
                "decision": "match",
                "similarity": 0.7412,
                "threshold": 0.53,
                "duration_seconds": 4.1
            },
            "model": {"name": "eidos", "version": "v1"}
        }

        mock_post.side_effect = [
            MockResponse(200, aletheia_body, headers={"X-Request-Id": "req_aletheia_001"}),
            MockResponse(200, eidos_body, headers={"X-Request-Id": "req_eidos_001"})
        ]

        # Use session recording (webm) as the mic audio for verification
        res = self.verifier.verify_all(
            audio_path=str(self.webm_file),
            candidate_id="candidate-001"
        )

        self.assertTrue(res["success"])
        self.assertTrue(res["cloud_verified"])
        self.assertEqual(res["overall_status"], "verified")
        self.assertFalse(res["has_audio_anomaly"])
        self.assertFalse(res["has_inconclusive"])
        self.assertFalse(res["has_error"])

        # Check Aletheia
        df = res["audio_deepfake"]
        self.assertEqual(df["status"], "verified")
        self.assertEqual(df["verdict"], "real")
        self.assertFalse(df["is_deepfake"])
        self.assertEqual(df["request_id"], "req_aletheia_001")

        # Check Eidos
        sp = res["speaker_verification"]
        self.assertEqual(sp["status"], "verified")
        self.assertEqual(sp["decision"], "match")
        self.assertTrue(sp["is_match"])
        self.assertEqual(sp["similarity"], 0.7412)
        self.assertEqual(sp["similarity_formatted"], "+0.7412")
        self.assertEqual(sp["request_id"], "req_eidos_001")

    # ── Test 2: Aletheia FAKE Detection ───────────────────────────────────────
    @patch("requests.post")
    def test_aletheia_fake_detection(self, mock_post):
        mock_post.return_value = MockResponse(200, {
            "request_id": "req_fake_123",
            "operation_id": "op_fake_123",
            "result": {
                "verdict": "fake",
                "label": "synthetic",
                "is_synthetic": True,
                "score": 0.9127,
                "threshold": 0.62,
                "duration_seconds": 8.0
            },
            "model": {"name": "aletheia"}
        })

        res = self.verifier.detect_deepfake(str(self.webm_file))
        self.assertEqual(res["status"], "flagged")
        self.assertEqual(res["verdict"], "fake")
        self.assertTrue(res["is_deepfake"])
        self.assertGreater(len(res["flag_reasons"]), 0)
        self.assertIn("synthetic speech", res["flag_reasons"][0])

    # ── Test 3: Aletheia NOT_SURE (Inconclusive) ──────────────────────────────
    @patch("requests.post")
    def test_aletheia_not_sure_inconclusive(self, mock_post):
        mock_post.return_value = MockResponse(200, {
            "request_id": "req_not_sure_456",
            "operation_id": "op_not_sure_456",
            "result": {
                "verdict": "not_sure",
                "label": "uncertain",
                "is_synthetic": False,
                "score": 0.58,
                "threshold": 0.62,
                "duration_seconds": 4.5
            },
            "model": {"name": "aletheia"}
        })

        res = self.verifier.detect_deepfake(str(self.webm_file))
        self.assertEqual(res["status"], "inconclusive")
        self.assertEqual(res["verdict"], "not_sure")
        self.assertFalse(res["is_deepfake"])
        # Must not be treated as a pass
        self.assertNotEqual(res["status"], "verified")
        self.assertIn("not_sure", res["flag_reasons"][0])

    # ── Test 4: Eidos NO_MATCH ────────────────────────────────────────────────
    @patch("requests.post")
    @patch("requests.get")
    def test_eidos_no_match(self, mock_get, mock_post):
        mock_get.return_value = MockResponse(200, {
            "speaker_id": "candidate-001",
            "samples": 1
        })
        mock_post.return_value = MockResponse(200, {
            "request_id": "req_nomatch_789",
            "operation_id": "op_nomatch_789",
            "speaker_id": "candidate-001",
            "result": {
                "decision": "no_match",
                "similarity": -0.1543,
                "threshold": 0.53,
                "duration_seconds": 5.0
            },
            "model": {"name": "eidos"}
        })

        # Use session mic audio (webm) for speaker verification
        res = self.verifier.verify_speaker(str(self.webm_file), candidate_id="candidate-001")
        self.assertEqual(res["status"], "flagged")
        self.assertEqual(res["decision"], "no_match")
        self.assertFalse(res["is_match"])
        self.assertEqual(res["similarity"], -0.1543)
        self.assertEqual(res["similarity_formatted"], "-0.1543")
        self.assertIn("NO_MATCH", res["flag_reasons"][0])

    # ── Test 5: Vendor Errors (401, 402, 403) Preserving Codes & IDs ──────────
    @patch("requests.post")
    def test_vendor_error_codes(self, mock_post):
        # Test 401 invalid_api_key
        mock_post.return_value = MockResponse(401, {
            "error": {
                "type": "authentication",
                "code": "invalid_api_key",
                "message": "The API key is invalid."
            },
            "request_id": "req_err_401"
        }, headers={"X-Request-Id": "req_err_401"})

        res = self.verifier.detect_deepfake(str(self.webm_file))
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["error_code"], "invalid_api_key")
        self.assertEqual(res["request_id"], "req_err_401")
        self.assertEqual(res["http_status"], 401)

        # Test 402 insufficient_credits
        mock_post.return_value = MockResponse(402, {
            "error": {
                "type": "insufficient_credits",
                "code": "insufficient_credits",
                "message": "Not enough credits are available for this operation."
            },
            "request_id": "req_err_402"
        }, headers={"X-Request-Id": "req_err_402"})

        res = self.verifier.detect_deepfake(str(self.webm_file))
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["error_code"], "insufficient_credits")
        self.assertEqual(res["http_status"], 402)

        # Test 403 insufficient_scope
        mock_post.return_value = MockResponse(403, {
            "error": {
                "type": "permission",
                "code": "insufficient_scope",
                "message": "This API key is missing required scope deepfake:detect."
            },
            "request_id": "req_err_403"
        })
        res = self.verifier.detect_deepfake(str(self.webm_file))
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["error_code"], "insufficient_scope")

    # ── Test 6: Network Timeout ───────────────────────────────────────────────
    @patch("requests.post")
    def test_timeout_handling(self, mock_post):
        mock_post.side_effect = requests.exceptions.Timeout("Connection timed out after 30s")

        res = self.verifier.detect_deepfake(str(self.webm_file))
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["error_code"], "timeout")
        self.assertFalse(res["is_deepfake"])

    # ── Test 7: Failed Enrollment Stops Execution ─────────────────────────────
    @patch("requests.post")
    @patch("requests.get")
    def test_failed_enrollment_stops_verify(self, mock_get, mock_post):
        # 1. Speaker lookup returns 404 (not enrolled)
        mock_get.return_value = MockResponse(404, {
            "error": {
                "type": "not_found",
                "code": "speaker_not_found",
                "message": "Speaker not found."
            },
            "request_id": "req_lookup_404"
        })

        # 2. Enrollment fails with 422 audio_too_short
        mock_post.return_value = MockResponse(422, {
            "error": {
                "type": "unprocessable_audio",
                "code": "audio_too_short",
                "message": "Enrollment audio must be at least 15 seconds long."
            },
            "request_id": "req_enroll_422"
        })

        # Use Test_Audio.mp4 as the test audio for speaker verification
        res = self.verifier.verify_speaker(str(self.test_audio_mp4), candidate_id="candidate-001")
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["error_code"], "audio_too_short")
        self.assertEqual(res["http_status"], 422)
        # Verify that requests.post was called only ONCE for enrollment, NOT for verify!
        self.assertEqual(mock_post.call_count, 1)
        enroll_url = mock_post.call_args[0][0]
        self.assertIn("/speakers/enroll", enroll_url)

    # ── Test 8: Malformed JSON and Schema Contradictions ──────────────────────
    @patch("requests.post")
    def test_malformed_json_and_contradictions(self, mock_post):
        # Malformed JSON
        mock_post.return_value = MockResponse(200, ValueError("Invalid JSON token"))
        res = self.verifier.detect_deepfake(str(self.webm_file))
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["error_code"], "invalid_json")

        # Missing verdict
        mock_post.return_value = MockResponse(200, {
            "request_id": "req_bad_1",
            "result": {
                "score": 0.5,
                "threshold": 0.62,
                "is_synthetic": False
            }
        })
        res = self.verifier.detect_deepfake(str(self.webm_file))
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["error_code"], "malformed_response")

        # Contradiction: verdict is fake but is_synthetic is False
        mock_post.return_value = MockResponse(200, {
            "request_id": "req_bad_2",
            "result": {
                "verdict": "fake",
                "score": 0.8,
                "threshold": 0.62,
                "is_synthetic": False
            }
        })
        res = self.verifier.detect_deepfake(str(self.webm_file))
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["error_code"], "malformed_response")
        self.assertIn("Contradiction", res["error"])

    # ── Test 9: Speaker ID Validation ─────────────────────────────────────────
    def test_speaker_id_validation(self):
        # Valid speaker IDs
        self.assertEqual(validate_speaker_id("candidate-001"), "candidate-001")
        self.assertEqual(validate_speaker_id("user_42.test:sub"), "user_42.test:sub")
        self.assertEqual(validate_speaker_id("A1-Z9"), "A1-Z9")

        # Invalid speaker IDs must raise ValueError without silent mutation
        with self.assertRaises(ValueError):
            validate_speaker_id("user/123")
        with self.assertRaises(ValueError):
            validate_speaker_id("candidate name with space")
        with self.assertRaises(ValueError):
            validate_speaker_id("user@email.com")
        with self.assertRaises(ValueError):
            validate_speaker_id("")
        with self.assertRaises(ValueError):
            validate_speaker_id("a" * 65)

    # ── Test 10: Audio MIME Type & Extension Integrity ────────────────────────
    def test_audio_mime_detection(self):
        mime, ext = detect_audio_mime_and_ext(str(self.webm_file))
        self.assertEqual(mime, "audio/webm")
        self.assertEqual(ext, ".webm")

        mime, ext = detect_audio_mime_and_ext(str(self.ogg_file))
        self.assertEqual(mime, "audio/ogg")
        self.assertEqual(ext, ".ogg")

        mime, ext = detect_audio_mime_and_ext(str(self.wav_file))
        self.assertEqual(mime, "audio/wav")
        self.assertEqual(ext, ".wav")

        # Detect Test_Audio.mp4 as video/mp4 (video container)
        mime, ext = detect_audio_mime_and_ext(str(self.test_audio_mp4))
        self.assertEqual(mime, "video/mp4")
        self.assertEqual(ext, ".mp4")

        # Detect .m4a file as audio/mp4
        m4a_dummy = self.dir_path / "sample.m4a"
        with open(m4a_dummy, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A isom")
        mime, ext = detect_audio_mime_and_ext(str(m4a_dummy))
        self.assertEqual(mime, "audio/mp4")
        self.assertEqual(ext, ".m4a")

        # Compressed file passed to local WAV decoder must fail cleanly
        with self.assertRaises(ValueError):
            self.verifier._read_wav_data(str(self.webm_file))
        with self.assertRaises(ValueError):
            self.verifier._read_wav_data(str(self.test_audio_mp4))

    # ── Test 11: Missing API Key Fails Closed (No Silent Fallback) ────────────
    def test_missing_api_key_fails_closed(self):
        no_key_verifier = DetectifAIVerifier(
            api_key="",
            api_url=self.mock_api_url,
            mode="cloud"
        )
        res = no_key_verifier.detect_deepfake(str(self.webm_file))
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["error_code"], "missing_api_key")
        self.assertFalse(res["is_cloud"])

    # ── Test 12: Local Debug Mode Explicitly Labeled ──────────────────────────
    def test_local_debug_mode_labeled(self):
        debug_verifier = DetectifAIVerifier(
            api_key="",
            supabase_gateway=self.mock_gateway,
            mode="local_debug"
        )
        res = debug_verifier.verify_all(
            audio_path=str(self.wav_file),
            candidate_id="candidate-001"
        )
        self.assertFalse(res["cloud_verified"])
        self.assertIn("Local Debug", res["audio_deepfake"]["model"])
        self.assertIn("Local Debug", res["speaker_verification"]["model"])
        self.assertEqual(res["audio_deepfake"]["status"], "local_debug_only")
        self.assertEqual(res["speaker_verification"]["status"], "local_debug_only")

    def _create_temp_wav(self, duration_sec=20.0):
        fd, temp_path = tempfile.mkstemp(prefix="mock_extracted_", suffix=".wav", dir=self.dir_path)
        os.close(fd)
        with wave.open(temp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * int(16000 * duration_sec))
        return temp_path

    # ── Test 13: MP4 with Usable Audio Extraction (Preprocesses to temporary WAV) ─
    def test_mp4_with_audio_extraction(self):
        # 1. Verify Test_Audio.mp4 contains an audio stream
        self.assertTrue(has_mp4_audio_stream(str(self.test_audio_mp4)))

        # 2. Mock extract_audio_from_mp4 to return a valid 20s WAV file
        temp_wav = self._create_temp_wav(20.0)
        with patch("detectifai_verifier.extract_audio_from_mp4", return_value=temp_wav) as mock_extract:
            prep_path, mime, is_temp = prepare_reference_audio(str(self.test_audio_mp4))
            mock_extract.assert_called_once_with(str(self.test_audio_mp4))
            self.assertEqual(mime, "audio/wav")
            self.assertTrue(is_temp)
            self.assertTrue(prep_path.endswith(".wav"))
            self.assertTrue(os.path.isfile(prep_path))

    # ── Test 14: Reference Audio Duration Bounds & Validation ────────────────
    def test_prepare_reference_audio_duration_bounds(self):
        # 1. Under 15s (5s WAV) must raise ValueError
        short_wav = self.dir_path / "short.wav"
        with wave.open(str(short_wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 16000 * 5)

        with self.assertRaises(ValueError) as ctx_short:
            prepare_reference_audio(str(short_wav))
        self.assertIn("outside the required 15-60s range", str(ctx_short.exception))

        # 2. Over 60s (65s WAV) must raise ValueError
        long_wav = self.dir_path / "long.wav"
        with wave.open(str(long_wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 16000 * 65)

        with self.assertRaises(ValueError) as ctx_long:
            prepare_reference_audio(str(long_wav))
        self.assertIn("outside the required 15-60s range", str(ctx_long.exception))

    # ── Test 15: MP4 Missing Audio Stream Fails ───────────────────────────────
    def test_mp4_without_audio_stream_rejected(self):
        no_audio_mp4 = self.dir_path / "video_only.mp4"
        with open(no_audio_mp4, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"vide" * 10)

        self.assertFalse(has_mp4_audio_stream(str(no_audio_mp4)))

        with self.assertRaises(ValueError) as ctx_no_audio:
            prepare_reference_audio(str(no_audio_mp4))
        self.assertIn("contains no usable audio stream", str(ctx_no_audio.exception))

        with self.assertRaises(ValueError) as ctx_extract:
            extract_audio_from_mp4(str(no_audio_mp4))
        self.assertIn("contains no usable audio stream", str(ctx_extract.exception))

    # ── Test 16: MP4 Extraction Failure (FFmpeg missing or failing) ───────────
    def test_mp4_extraction_failure_ffmpeg_missing_or_error(self):
        # 1. FFmpeg missing raises RuntimeError
        with patch("shutil.which", return_value=None), patch.dict(os.environ, {"FFMPEG_PATH": ""}, clear=False):
            with self.assertRaises(RuntimeError) as ctx_missing:
                extract_audio_from_mp4(str(self.test_audio_mp4))
            self.assertIn("FFmpeg is required", str(ctx_missing.exception))

        # 2. FFmpeg non-zero exit code raises RuntimeError
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "Invalid data found when processing input"
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), patch("subprocess.run", return_value=mock_proc):
            with self.assertRaises(RuntimeError) as ctx_fail:
                extract_audio_from_mp4(str(self.test_audio_mp4))
            self.assertIn("FFmpeg audio extraction failed", str(ctx_fail.exception))

        # 3. Extraction failure inside verify_speaker fails closed with reference_audio_invalid
        with patch("detectifai_verifier.prepare_reference_audio", side_effect=RuntimeError("FFmpeg missing")):
            with patch("requests.get", return_value=MockResponse(404, {"error": {"code": "speaker_not_found"}})):
                res = self.verifier.verify_speaker(
                    test_audio_path=str(self.webm_file),
                    candidate_id="candidate-001",
                    local_reference_path=str(self.test_audio_mp4)
                )
                self.assertEqual(res["status"], "error")
                self.assertEqual(res["decision"], "error")
                self.assertEqual(res["error_code"], "reference_audio_invalid")
                self.assertFalse(res["is_match"])

    # ── Test 17: Eidos Enrollment & Verification - MATCH (with temp cleanup) ──
    @patch("requests.post")
    @patch("requests.get")
    def test_eidos_enrollment_and_verification_match(self, mock_get, mock_post):
        # Candidate not enrolled yet -> GET returns 404
        mock_get.return_value = MockResponse(404, {
            "error": {"type": "not_found", "code": "speaker_not_found", "message": "Speaker not found."}
        })

        # POST #1: Enrollment with prepared WAV -> 201 Created
        enroll_response = MockResponse(201, {
            "request_id": "req_enroll_success_01",
            "operation_id": "op_enroll_success_01",
            "speaker": {
                "speaker_id": "candidate-001",
                "display_name": "Candidate candidate-001",
                "samples": 1
            }
        })

        # POST #2: Verification with 25s session mic clip -> 200 Match
        verify_response = MockResponse(200, {
            "request_id": "req_verify_success_01",
            "operation_id": "op_verify_success_01",
            "speaker_id": "candidate-001",
            "result": {
                "decision": "match",
                "similarity": 0.7812,
                "threshold": 0.53,
                "duration_seconds": 25.0
            },
            "model": {"name": "eidos", "version": "v1"}
        })

        mock_post.side_effect = [enroll_response, verify_response]

        temp_wav = self._create_temp_wav(20.0)
        with patch("detectifai_verifier.extract_audio_from_mp4", return_value=temp_wav):
            res = self.verifier.verify_speaker(
                test_audio_path=str(self.webm_file),
                candidate_id="candidate-001",
                local_reference_path=str(self.test_audio_mp4)
            )

        self.assertEqual(res["status"], "verified")
        self.assertEqual(res["decision"], "match")
        self.assertTrue(res["is_match"])
        self.assertEqual(res["similarity"], 0.7812)
        self.assertEqual(res["similarity_formatted"], "+0.7812")
        self.assertEqual(res["threshold"], 0.53)
        self.assertEqual(res["request_id"], "req_verify_success_01")
        self.assertEqual(res["operation_id"], "op_verify_success_01")

        # Verify exact calls: exactly 2 POST calls
        self.assertEqual(mock_post.call_count, 2)

        # Call 1: Enrollment POST
        call1_url = mock_post.call_args_list[0][0][0]
        call1_kwargs = mock_post.call_args_list[0][1]
        self.assertIn("/speakers/enroll", call1_url)
        self.assertEqual(call1_kwargs["data"]["speaker_id"], "candidate-001")
        enroll_file_tuple = call1_kwargs["files"]["file"]
        self.assertTrue(enroll_file_tuple[0].endswith(".wav"))
        self.assertEqual(enroll_file_tuple[2], "audio/wav")

        # Call 2: Verify POST
        call2_url = mock_post.call_args_list[1][0][0]
        call2_kwargs = mock_post.call_args_list[1][1]
        self.assertIn("/speakers/verify", call2_url)
        self.assertEqual(call2_kwargs["data"]["speaker_id"], "candidate-001")
        verify_file_tuple = call2_kwargs["files"]["file"]
        self.assertEqual(verify_file_tuple[0], "test.webm")
        self.assertEqual(verify_file_tuple[2], "audio/webm")

        # Verify temporary extracted WAV file was safely cleaned up!
        self.assertFalse(os.path.exists(temp_wav), "Temporary extracted WAV must be deleted after verification")

    # ── Test 18: Eidos Enrollment & Verification - NO_MATCH (with temp cleanup) ──
    @patch("requests.post")
    @patch("requests.get")
    def test_eidos_enrollment_and_verification_no_match(self, mock_get, mock_post):
        mock_get.return_value = MockResponse(404, {"error": {"code": "speaker_not_found"}})
        enroll_response = MockResponse(201, {
            "request_id": "req_enroll_02",
            "speaker": {"speaker_id": "candidate-001", "samples": 1}
        })
        verify_response = MockResponse(200, {
            "request_id": "req_verify_nomatch",
            "speaker_id": "candidate-001",
            "result": {
                "decision": "no_match",
                "similarity": -0.1543,
                "threshold": 0.53,
                "duration_seconds": 25.0
            },
            "model": {"name": "eidos", "version": "v1"}
        })
        mock_post.side_effect = [enroll_response, verify_response]

        temp_wav = self._create_temp_wav(20.0)
        with patch("detectifai_verifier.extract_audio_from_mp4", return_value=temp_wav):
            res = self.verifier.verify_speaker(
                test_audio_path=str(self.webm_file),
                candidate_id="candidate-001",
                local_reference_path=str(self.test_audio_mp4)
            )

        self.assertEqual(res["status"], "flagged")
        self.assertEqual(res["decision"], "no_match")
        self.assertFalse(res["is_match"])
        self.assertEqual(res["similarity"], -0.1543)
        self.assertEqual(res["similarity_formatted"], "-0.1543")
        self.assertIn("NO_MATCH", res["flag_reasons"][0])
        # Verify temporary file cleaned up
        self.assertFalse(os.path.exists(temp_wav))

    # ── Test 19: Eidos Enrollment Failure Stops Verification & Cleans Temp ────
    @patch("requests.post")
    @patch("requests.get")
    def test_eidos_enrollment_failure_stops_verification(self, mock_get, mock_post):
        mock_get.return_value = MockResponse(404, {"error": {"code": "speaker_not_found"}})
        # Enrollment fails with 422 Unprocessable Entity
        mock_post.return_value = MockResponse(422, {
            "error": {
                "type": "invalid_request",
                "code": "audio_insufficient_speech",
                "message": "Reference audio contains insufficient voiced speech."
            }
        })

        temp_wav = self._create_temp_wav(20.0)
        with patch("detectifai_verifier.extract_audio_from_mp4", return_value=temp_wav):
            res = self.verifier.verify_speaker(
                test_audio_path=str(self.webm_file),
                candidate_id="candidate-001",
                local_reference_path=str(self.test_audio_mp4)
            )

        self.assertEqual(res["status"], "error")
        self.assertEqual(res["decision"], "error")
        self.assertFalse(res["is_match"])
        self.assertEqual(res["error_code"], "audio_insufficient_speech")
        # Verify call was stopped: only 1 POST call (enrollment), verify POST never executed
        self.assertEqual(mock_post.call_count, 1)
        # Verify temporary file cleaned up
        self.assertFalse(os.path.exists(temp_wav))

    # ── Test 20: Direct Video Container Upload Rejected Fail-Closed ───────────
    def test_video_container_direct_upload_rejected(self):
        # 1. detect_deepfake rejects video/mp4
        res_df = self.verifier.detect_deepfake(str(self.test_audio_mp4))
        self.assertEqual(res_df["status"], "error")
        self.assertEqual(res_df["verdict"], "error")
        self.assertEqual(res_df["error_code"], "unsupported_media_type")

        # 2. enroll_speaker rejects video/mp4
        res_enroll = self.verifier.enroll_speaker(str(self.test_audio_mp4), "candidate-001")
        self.assertFalse(res_enroll["success"])
        self.assertEqual(res_enroll["error_code"], "unsupported_media_type")

        # 3. _post_speaker_verify rejects video/mp4
        res_verify = self.verifier._post_speaker_verify(str(self.test_audio_mp4), "candidate-001")
        self.assertEqual(res_verify["status"], "error")
        self.assertEqual(res_verify["decision"], "error")
        self.assertEqual(res_verify["error_code"], "unsupported_media_type")

    # ── Test 21: Unsupported / Invalid Reference Media Fails Closed ───────────
    @patch("requests.get")
    def test_unsupported_reference_media_fails_closed(self, mock_get):
        mock_get.return_value = MockResponse(404, {
            "error": {"type": "not_found", "code": "speaker_not_found", "message": "Speaker not found."}
        })
        text_file = self.dir_path / "reference.txt"
        with open(text_file, "w") as f:
            f.write("This is plain text, not audio.")

        res = self.verifier.verify_speaker(
            test_audio_path=str(self.webm_file),
            candidate_id="candidate-001",
            local_reference_path=str(text_file)
        )
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["error_code"], "reference_audio_invalid")
        self.assertFalse(res["is_match"])
        self.assertEqual(res["decision"], "error")

    # ── Test 22: Eidos Verify API Timeout Fails Closed ────────────────────────
    @patch("requests.post")
    @patch("requests.get")
    def test_eidos_verify_timeout(self, mock_get, mock_post):
        mock_get.return_value = MockResponse(200, {"speaker_id": "candidate-001", "samples": 1})
        mock_post.side_effect = requests.exceptions.Timeout("Eidos verify timed out after 300s")

        res = self.verifier.verify_speaker(
            test_audio_path=str(self.webm_file),
            candidate_id="candidate-001"
        )
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["error_code"], "timeout")
        self.assertEqual(res["decision"], "error")
        self.assertFalse(res["is_match"])

    # ── Test 23: Malformed Eidos Verification Responses ───────────────────────
    @patch("requests.post")
    @patch("requests.get")
    def test_malformed_eidos_responses(self, mock_get, mock_post):
        mock_get.return_value = MockResponse(200, {"speaker_id": "candidate-001", "samples": 1})

        # Case 1: Missing result object
        mock_post.return_value = MockResponse(200, {
            "request_id": "req_mal_01",
            "speaker_id": "candidate-001"
        })
        res1 = self.verifier.verify_speaker(str(self.webm_file), candidate_id="candidate-001")
        self.assertEqual(res1["status"], "error")
        self.assertEqual(res1["error_code"], "malformed_response")

        # Case 2: Similarity out of [-1.0, 1.0] range
        mock_post.return_value = MockResponse(200, {
            "request_id": "req_mal_02",
            "speaker_id": "candidate-001",
            "result": {
                "decision": "match",
                "similarity": 2.5,
                "threshold": 0.53
            }
        })
        res2 = self.verifier.verify_speaker(str(self.webm_file), candidate_id="candidate-001")
        self.assertEqual(res2["status"], "error")
        self.assertEqual(res2["error_code"], "malformed_response")

        # Case 3: Invalid decision string
        mock_post.return_value = MockResponse(200, {
            "request_id": "req_mal_03",
            "speaker_id": "candidate-001",
            "result": {
                "decision": "maybe_match",
                "similarity": 0.5,
                "threshold": 0.53
            }
        })
        res3 = self.verifier.verify_speaker(str(self.webm_file), candidate_id="candidate-001")
        self.assertEqual(res3["status"], "error")
        self.assertEqual(res3["error_code"], "malformed_response")


if __name__ == "__main__":
    unittest.main()
