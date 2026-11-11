"""Classify a local image, or optionally run a user-started OpenCV webcam preview."""

import argparse
import json
import os
import sys
from pathlib import Path

import torch
from PIL import Image

from .data import square_image
from .model import image_transform, load_checkpoint


def predict_image(model, checkpoint, image, device, top_k=3):
    tensor = image_transform(checkpoint["image_size"])(square_image(image, checkpoint["cache_size"]))
    with torch.inference_mode():
        scores = model(tensor.unsqueeze(0).to(device)).softmax(1)[0]
    values, indices = scores.topk(min(top_k, len(checkpoint["labels"])))
    return [dict(checkpoint["labels"][index], score=float(value)) for index, value in zip(indices.cpu().tolist(), values.cpu().tolist())]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--image")
    parser.add_argument("--camera", type=int, help="Optional webcam index; requires opencv-python. Press Space to classify and Q to quit.")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    args = parser.parse_args()
    if (args.image is None) == (args.camera is None) or args.top_k < 1:
        parser.error("Choose exactly one of --image or --camera; top-k must be positive.")
    torch.set_num_threads(min(8, os.cpu_count() or 1))
    device = torch.device("cuda" if args.device == "auto" and torch.cuda.is_available() else "cpu" if args.device == "auto" else args.device)
    model, checkpoint = load_checkpoint(args.checkpoint, device)
    if args.image:
        with Image.open(Path(args.image)) as image:
            predictions = predict_image(model, checkpoint, image, device, args.top_k)
        print(json.dumps({"image": args.image, "predictions": predictions, "note": "Closed-set scores; unknown herbs are not reliably rejected."}, ensure_ascii=False, indent=2))
        return
    try:
        import cv2
    except ImportError as error:
        raise SystemExit("Webcam preview needs opencv-python; image inference works without it.") from error
    capture = cv2.VideoCapture(args.camera)
    if not capture.isOpened():
        capture.release()
        raise SystemExit(f"Cannot open camera {args.camera}")
    caption = "Space: classify | Q: quit"
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            display = frame.copy()
            cv2.putText(display, caption, (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 180, 0), 2)
            cv2.imshow("TCM herb recognition", display)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == 32:
                predictions = predict_image(model, checkpoint, Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)), device, args.top_k)
                print(json.dumps(predictions, ensure_ascii=False, indent=2), flush=True)
                caption = f"{predictions[0]['name']} {predictions[0]['score']:.1%}"
    finally:
        capture.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
