"""
Supabase Audio Gateway for RyouU Remote Interview Integrity Suite.

Provides secure fetching, caching, and local-debug fallback for candidate reference
voice samples used in speaker verification against Detectif.ai Eidos.
"""

import os
import sys
import json
import argparse
import traceback
from pathlib import Path

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

class SupabaseAudioGateway:
    def __init__(self, supabase_url=None, supabase_key=None, bucket=None, table=None, cache_dir=None, debug_dir=None):
        self.supabase_url = (supabase_url or os.getenv("SUPABASE_URL") or "").rstrip("/")
        self.supabase_key = supabase_key or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY") or ""
        self.bucket = bucket or os.getenv("SUPABASE_BUCKET") or "kyc-reference-audio"
        self.table = table or os.getenv("SUPABASE_TABLE") or "candidates"
        
        # Local paths
        base_dir = Path(__file__).resolve().parent.parent.parent
        self.cache_dir = Path(cache_dir) if cache_dir else base_dir / "storage" / "reference_cache"
        self.debug_dir = Path(debug_dir) if debug_dir else base_dir / "debug_audio"

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.debug_dir.mkdir(parents=True, exist_ok=True)

    def is_configured(self):
        """Returns True if Supabase credentials are provided."""
        return bool(self.supabase_url and self.supabase_key)

    def get_auth_headers(self):
        return {
            "apikey": self.supabase_key,
            "Authorization": f"Bearer {self.supabase_key}"
        }

    def test_connection(self):
        """Tests connectivity to Supabase project."""
        if not self.is_configured():
            return {
                "success": False,
                "configured": False,
                "message": "Supabase URL or Key is not configured. Running in Local Debug Mode."
            }

        try:
            # Check Storage API or Rest API
            url = f"{self.supabase_url}/storage/v1/bucket"
            resp = requests.get(url, headers=self.get_auth_headers(), timeout=5)
            if resp.status_code in (200, 201):
                return {
                    "success": True,
                    "configured": True,
                    "message": "Connected successfully to Supabase Storage."
                }
            elif resp.status_code == 401 or resp.status_code == 403:
                return {
                    "success": False,
                    "configured": True,
                    "status_code": resp.status_code,
                    "message": "Supabase authentication failed. Check your SUPABASE_KEY / SUPABASE_ANON_KEY."
                }
            else:
                return {
                    "success": False,
                    "configured": True,
                    "status_code": resp.status_code,
                    "message": f"Supabase responded with HTTP {resp.status_code}: {resp.text[:200]}"
                }
        except Exception as e:
            return {
                "success": False,
                "configured": True,
                "message": f"Connection error: {str(e)}"
            }

    def fetch_reference_audio(self, candidate_id, local_override_path=None):
        """
        Fetches reference audio file for speaker verification.
        Prioritizes:
        1. Explicit local_override_path (if provided and exists)
        2. Supabase Storage / Database download (if configured)
        3. Local debug_audio directory fallback
        """
        # 1. Local override check
        if local_override_path:
            override_p = Path(local_override_path)
            if not override_p.is_absolute() and not override_p.exists():
                cand = Path(__file__).resolve().parent.parent.parent / local_override_path
                if cand.exists():
                    override_p = cand
            if override_p.exists():
                return {
                    "success": True,
                    "source": "local_override",
                    "file_path": str(override_p.resolve()),
                    "candidate_id": candidate_id,
                    "message": f"Loaded local reference audio: {override_p.name}"
                }
            else:
                return {
                    "success": False,
                    "source": "local_override",
                    "error": f"Explicit local reference audio file not found: {local_override_path}",
                    "candidate_id": candidate_id
                }

        # 2. Supabase check if configured
        if self.is_configured():
            try:
                cached_file = self.cache_dir / f"{candidate_id}_reference.wav"
                
                # Check if we should query database table for candidate storage path or URL
                object_path = f"{candidate_id}.wav"
                db_url = f"{self.supabase_url}/rest/v1/{self.table}?candidate_id=eq.{candidate_id}&select=*"
                db_resp = requests.get(db_url, headers=self.get_auth_headers(), timeout=5)
                
                if db_resp.status_code == 200:
                    records = db_resp.json()
                    if records and isinstance(records, list) and len(records) > 0:
                        row = records[0]
                        # Look for potential audio path columns
                        possible_keys = ["reference_audio_url", "audio_url", "audio_path", "reference_audio_path", "voice_sample_path"]
                        for k in possible_keys:
                            if k in row and row[k]:
                                object_path = row[k]
                                break

                # Download from Supabase Storage
                if object_path.startswith("http://") or object_path.startswith("https://"):
                    storage_url = object_path
                else:
                    storage_url = f"{self.supabase_url}/storage/v1/object/authenticated/{self.bucket}/{object_path}"

                # Request download
                download_resp = requests.get(storage_url, headers=self.get_auth_headers(), timeout=10)
                if download_resp.status_code != 200:
                    # Try public object url
                    public_url = f"{self.supabase_url}/storage/v1/object/public/{self.bucket}/{object_path}"
                    download_resp = requests.get(public_url, timeout=10)

                if download_resp.status_code == 200 and len(download_resp.content) > 100:
                    with open(cached_file, "wb") as f:
                        f.write(download_resp.content)

                    return {
                        "success": True,
                        "source": "supabase",
                        "file_path": str(cached_file.resolve()),
                        "candidate_id": candidate_id,
                        "size": len(download_resp.content),
                        "message": f"Successfully fetched reference audio from Supabase bucket '{self.bucket}'"
                    }
                else:
                    print(f"[SupabaseGateway] File not found in Supabase ({download_resp.status_code}), checking local debug.", file=sys.stderr)
            except Exception as e:
                print(f"[SupabaseGateway] Supabase fetch error: {str(e)}, falling back to local.", file=sys.stderr)

        # 3. Local debug fallback
        debug_candidates = [
            self.debug_dir / f"{candidate_id}_reference.wav",
            self.debug_dir / f"{candidate_id}.wav",
            self.debug_dir / "candidate-001_reference.wav",
            self.debug_dir / "default_reference.wav"
        ]

        # Also search debug_dir for any .wav files
        for f in self.debug_dir.glob("*.wav"):
            if f not in debug_candidates:
                debug_candidates.append(f)

        for candidate_path in debug_candidates:
            if candidate_path.exists() and candidate_path.stat().st_size > 0:
                return {
                    "success": True,
                    "source": "local_debug",
                    "file_path": str(candidate_path.resolve()),
                    "candidate_id": candidate_id,
                    "message": f"Using local debug reference audio: {candidate_path.name}"
                }

        return {
            "success": False,
            "error": f"No reference audio found for candidate '{candidate_id}'. Neither in Supabase nor in local debug_audio/.",
            "candidate_id": candidate_id
        }

    def upload_reference_audio(self, candidate_id, file_path):
        """Uploads a local reference audio file to Supabase storage for the candidate."""
        if not self.is_configured():
            return {
                "success": False,
                "error": "Supabase URL or Key not configured."
            }

        file_p = Path(file_path)
        if not file_p.exists():
            return {"success": False, "error": f"File not found: {file_path}"}

        filename = f"{candidate_id}.wav"
        url = f"{self.supabase_url}/storage/v1/object/{self.bucket}/{filename}"

        headers = self.get_auth_headers()
        headers["Content-Type"] = "audio/wav"

        with open(file_p, "rb") as f:
            data = f.read()

        resp = requests.post(url, headers=headers, data=data, timeout=15)
        if resp.status_code in (200, 201):
            return {
                "success": True,
                "message": f"Uploaded reference audio to Supabase bucket '{self.bucket}' as '{filename}'",
                "object_path": filename
            }
        else:
            return {
                "success": False,
                "error": f"Upload failed ({resp.status_code}): {resp.text}"
            }

    def list_available_audio(self):
        """Lists available reference audio files in Supabase and local debug directory."""
        local_files = []
        if self.debug_dir.exists():
            for f in self.debug_dir.glob("*.wav"):
                local_files.append({
                    "name": f.name,
                    "path": str(f.resolve()),
                    "size": f.stat().st_size,
                    "source": "local_debug"
                })

        supabase_files = []
        if self.is_configured():
            try:
                url = f"{self.supabase_url}/storage/v1/object/list/{self.bucket}"
                resp = requests.post(url, headers=self.get_auth_headers(), json={"prefix": "", "limit": 50}, timeout=5)
                if resp.status_code == 200:
                    for item in resp.json():
                        supabase_files.append({
                            "name": item.get("name"),
                            "size": item.get("metadata", {}).get("size", 0),
                            "source": "supabase"
                        })
            except Exception as e:
                pass

        return {
            "success": True,
            "supabase_configured": self.is_configured(),
            "local_files": local_files,
            "supabase_files": supabase_files
        }

def main():
    parser = argparse.ArgumentParser(description="Supabase Audio Gateway for RyouU")
    parser.add_argument("--candidate-id", type=str, default="candidate-001", help="Candidate identifier")
    parser.add_argument("--fetch", action="store_true", help="Fetch reference audio for candidate")
    parser.add_argument("--test-connection", action="store_true", help="Test Supabase connection")
    parser.add_argument("--list", action="store_true", help="List available reference audio files")
    parser.add_argument("--local-audio", type=str, default=None, help="Local override audio file path")
    parser.add_argument("--supabase-url", type=str, default=None, help="Supabase project URL")
    parser.add_argument("--supabase-key", type=str, default=None, help="Supabase API key")
    parser.add_argument("--bucket", type=str, default=None, help="Supabase Storage bucket name")

    args = parser.parse_args()

    gateway = SupabaseAudioGateway(
        supabase_url=args.supabase_url,
        supabase_key=args.supabase_key,
        bucket=args.bucket
    )

    if args.test_connection:
        result = gateway.test_connection()
        print(json.dumps(result))
    elif args.list:
        result = gateway.list_available_audio()
        print(json.dumps(result))
    elif args.fetch or args.candidate_id:
        result = gateway.fetch_reference_audio(args.candidate_id, local_override_path=args.local_audio)
        print(json.dumps(result))
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
