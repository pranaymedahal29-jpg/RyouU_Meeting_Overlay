"""
CLIP ViT-L/14 Deepfake Verification Module for RyouU.

Strict error-based deepfake verification using OpenAI's CLIP ViT-L/14 vision-language
model. Zero fallback heuristics. If the model or input fails, raises a structured error.
"""

import sys
import os

# Suppress HuggingFace and Transformers noise for clean JSON output
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["TRANSFORMERS_VERBOSITY"] = "error"
os.environ["HF_HUB_VERBOSITY"] = "error"

try:
    from transformers.utils.logging import disable_progress_bar
    disable_progress_bar()
except Exception:
    pass

import json
import argparse
import traceback
import numpy as np

# Set stdout encoding to utf-8
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def run_clip_vit_l14_inference(video_path, max_frames=12):
    """
    Extracts keyframes from the video clip and performs balanced zero-shot
    classification between authentic webcam video and AI-generated deepfake video
    using CLIP ViT-L/14. Zero fallback heuristics.
    """
    try:
        import cv2
        from PIL import Image
        from transformers import CLIPProcessor, CLIPModel
        import torch
    except ImportError as exc:
        raise RuntimeError(f"Required dependency unavailable: {exc}") from exc

    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"Video file does not exist: {video_path}")

    # Extract keyframes from video
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"Could not open video file: {video_path}")

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    step = max(1, total_frames // max_frames) if total_frames > 0 else 10
    pil_images = []

    current_idx = 0
    while len(pil_images) < max_frames:
        ret, frame = cap.read()
        if not ret:
            break
        if current_idx % step == 0:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil_images.append(Image.fromarray(rgb))
        current_idx += 1
    cap.release()

    if not pil_images:
        raise ValueError("Video contains no readable frames for verification.")

    device = "cuda" if torch.cuda.is_available() else "cpu"

    # Balanced contrastive prompts: equal count, clear semantics without
    # penalizing natural webcam motion blur or video compression.
    real_prompts = [
        "an authentic live webcam video recording of a real person",
        "a genuine webcam video of a real person in a video call",
        "an authentic natural video of a human face"
    ]
    fake_prompts = [
        "an AI generated deepfake synthetic video of a face",
        "a computer generated deepfake video face swap",
        "a synthetic AI avatar face deepfake video"
    ]

    all_prompts = real_prompts + fake_prompts

    model_name = "openai/clip-vit-large-patch14"
    try:
        processor = CLIPProcessor.from_pretrained(model_name)
        model = CLIPModel.from_pretrained(model_name).to(device)
    except Exception as exc:
        raise RuntimeError(f"Failed to load CLIP model '{model_name}': {exc}") from exc

    try:
        inputs = processor(
            text=all_prompts,
            images=pil_images,
            return_tensors="pt",
            padding=True
        ).to(device)

        with torch.no_grad():
            outputs = model(**inputs)
            logits_per_image = outputs.logits_per_image  # shape: (num_frames, 6)
            probs = logits_per_image.softmax(dim=-1).cpu().numpy()
    except Exception as exc:
        raise RuntimeError(f"CLIP inference execution failed: {exc}") from exc

    # Compute binary normalized class probabilities across frames
    num_real = len(real_prompts)
    frame_real_mass = probs[:, :num_real].sum(axis=-1)
    frame_fake_mass = probs[:, num_real:].sum(axis=-1)

    total_mass = frame_real_mass + frame_fake_mass + 1e-10
    frame_fake_probs = frame_fake_mass / total_mass
    frame_real_probs = frame_real_mass / total_mass

    avg_real_prob = float(np.mean(frame_real_probs))
    median_real_prob = float(np.median(frame_real_probs))
    min_real_prob = float(np.min(frame_real_probs))
    avg_fake_prob = float(np.mean(frame_fake_probs))
    median_fake_prob = float(np.median(frame_fake_probs))
    max_fake_prob = float(np.max(frame_fake_probs))
    prob_variance = float(np.var(frame_real_probs))

    # User requirement: 70% confidence threshold to categorize the video as real
    REAL_CONFIDENCE_THRESHOLD = 0.70
    is_real = (avg_real_prob >= REAL_CONFIDENCE_THRESHOLD) and (median_real_prob >= 0.65)
    is_anomaly = not is_real

    flag_reasons = []
    if is_anomaly:
        if avg_real_prob < REAL_CONFIDENCE_THRESHOLD:
            flag_reasons.append(
                f"Video real confidence ({round(avg_real_prob * 100)}%) is below the required {int(REAL_CONFIDENCE_THRESHOLD * 100)}% threshold"
            )
        if avg_fake_prob >= 0.50:
            flag_reasons.append(
                f"CLIP ViT-L/14 detected AI deepfake synthetic characteristics (Fake score: {round(avg_fake_prob * 100)}%)"
            )

    return {
        "model": "OpenAI CLIP ViT-L/14 (PyTorch / Transformers)",
        "real_confidence": round(avg_real_prob, 4),
        "real_threshold": REAL_CONFIDENCE_THRESHOLD,
        "is_real": is_real,
        "is_anomaly": is_anomaly,
        "anomaly_score": round(avg_fake_prob, 4),
        "median_score": round(median_fake_prob, 4),
        "max_frame_anomaly": round(max_fake_prob, 4),
        "min_real_prob": round(min_real_prob, 4),
        "temporal_variance": round(prob_variance, 4),
        "confidence": round(avg_real_prob if is_real else avg_fake_prob, 3),
        "frames_analyzed": len(pil_images),
        "flag_reasons": flag_reasons
    }


def main():
    parser = argparse.ArgumentParser(description="Strict error-based deepfake verification using CLIP ViT-L/14")
    parser.add_argument("--video", required=True, help="Path to WebM video clip")
    parser.add_argument("--max-frames", type=int, default=12, help="Number of keyframes to analyze")
    args = parser.parse_args()

    video_path = os.path.abspath(args.video)
    if not os.path.exists(video_path):
        result = {
            "success": False,
            "error_code": "FILE_NOT_FOUND",
            "error": f"Video file not found: {video_path}"
        }
        print(json.dumps(result))
        sys.exit(1)

    try:
        analysis = run_clip_vit_l14_inference(video_path, max_frames=args.max_frames)
        output = {
            "success": True,
            "video_path": video_path,
            **analysis
        }
        print(json.dumps(output))
    except Exception as e:
        # Strict error-based output with zero fallbacks
        result = {
            "success": False,
            "error_code": "CLIP_MODEL_FAILED",
            "error": f"{type(e).__name__}: {str(e)}",
            "traceback": traceback.format_exc()
        }
        print(json.dumps(result))
        sys.exit(1)


if __name__ == "__main__":
    main()
