#!/usr/bin/env python3
"""YOLO11s vs YOLO26s person/vehicle benchmark. Does not control drones.

Usage:
  python test_yolo26_compare.py --video videos/example.mp4
  python test_yolo26_compare.py --video videos/example.mp4 --frames 60 --device mps

Both models see the SAME sampled frames; output is an indicative speed/count comparison,
NOT an accuracy benchmark without ground-truth labels.
"""
import argparse
import csv
import json
import statistics
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

CLASSES = {0: "person", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}


def main():
    parser = argparse.ArgumentParser(description="Compare YOLO11s and YOLO26s using identical video frames")
    parser.add_argument("--video", required=True, help="Local video file path")
    parser.add_argument("--frames", type=int, default=60, help="Number of frames per model (default: 60)")
    parser.add_argument("--every", type=int, default=10, help="Use one frame per N frames (default: 10)")
    parser.add_argument("--imgsz", type=int, default=832)
    parser.add_argument("--conf", type=float, default=0.35)
    parser.add_argument("--device", choices=["auto", "mps", "cpu"], default="auto")
    parser.add_argument("--output", default="reports/yolo26_comparison")
    args = parser.parse_args()

    if args.frames < 2 or args.every < 1 or args.imgsz < 32 or not 0 < args.conf < 1:
        parser.error("frames >= 2, every >= 1, imgsz >= 32, conf between 0 and 1 required")

    # Import after argument parsing so --help works without dependencies.
    try:
        import cv2
        import torch
        from ultralytics import YOLO
    except ImportError as exc:
        parser.error(f"필요한 패키지가 없어: {exc}. vision-ai 환경에서 pip install -U ultralytics 해줘")

    video = Path(args.video).expanduser().resolve()
    if not video.is_file():
        parser.error(f"영상 파일 없음: {video} (프로젝트 videos/에서 실제 파일명 확인)")
    device = ("mps" if torch.backends.mps.is_available() else "cpu") if args.device == "auto" else args.device
    if device == "mps" and not torch.backends.mps.is_available():
        parser.error("이 PyTorch 환경은 MPS를 지원하지 않아. --device cpu 사용")
    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        parser.error(f"영상 열기 실패: {video}")
    frames = []
    count = 0
    while len(frames) < args.frames:
        ok, frame = cap.read()
        if not ok:
            break
        if count % args.every == 0:
            frames.append(frame.copy())
        count += 1
    cap.release()
    if len(frames) < 2:
        parser.error("영상에서 2개 이상의 프레임을 읽지 못했어")
    print(f"[VIDEO] {video.name} | {len(frames)} sampled frames | device={device}")
    rows = []
    summaries = {}
    for model_name in ("yolo11s.pt", "yolo26s.pt"):
        print(f"\n[MODEL] loading {model_name} (최초 실행 시 모델 다운로드 필요)")
        try:
            model = YOLO(model_name)
        except Exception as exc:
            print(f"[ERROR] {model_name} 로딩 실패: {exc}\nYOLO26 지원 최신 ultralytics를 확인해 줘: python -m pip install -U ultralytics")
            raise SystemExit(2)
        # Warm-up before measuring; don't count first-run compilation/initialization.
        try:
            model.predict(frames[0], device=device, imgsz=args.imgsz, conf=args.conf,
                          classes=list(CLASSES), verbose=False)
            if device == "mps":
                torch.mps.synchronize()
        except Exception as exc:
            print(f"[ERROR] {model_name} 추론 실패: {exc}")
            raise SystemExit(2)
        times = []
        totals = Counter()
        for index, frame in enumerate(frames):
            if device == "mps":
                torch.mps.synchronize()
            start = time.perf_counter()
            prediction = model.predict(frame, device=device, imgsz=args.imgsz,
                                       conf=args.conf, classes=list(CLASSES), verbose=False)[0]
            if device == "mps":
                torch.mps.synchronize()
            elapsed = (time.perf_counter() - start) * 1000
            times.append(elapsed)
            counts = Counter()
            for box in prediction.boxes:
                cls = int(box.cls.item())
                if cls in CLASSES:
                    counts[CLASSES[cls]] += 1
            totals.update(counts)
            rows.append({"model": model_name, "sample_index": index, "inference_ms": round(elapsed, 2),
                         **{k: counts[k] for k in CLASSES.values()}})
            if index == 0:
                image_path = output / f"sample_{model_name.replace('.pt', '')}.jpg"
                cv2.imwrite(str(image_path), prediction.plot())
            if (index + 1) % 10 == 0 or index + 1 == len(frames):
                print(f"  frames {index + 1}/{len(frames)}", flush=True)
        avg_ms = statistics.mean(times)
        summaries[model_name] = {"mean_ms": round(avg_ms, 2),
                                 "median_ms": round(statistics.median(times), 2),
                                 "inference_fps": round(1000 / avg_ms, 2),
                                 "total_detections": dict(totals),
                                 "frames": len(frames)}
        print(f"[RESULT] {model_name}: mean={avg_ms:.1f} ms | estimated inference FPS={1000/avg_ms:.1f}")
        del model
        if device == "mps":
            torch.mps.empty_cache()

    with (output / "frame_results.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["model", "sample_index", "inference_ms", *CLASSES.values()])
        writer.writeheader()
        writer.writerows(rows)
    report = {"created_at": datetime.now().isoformat(timespec="seconds"), "video": str(video),
              "device": device, "imgsz": args.imgsz, "confidence": args.conf,
              "sample_every_n_frames": args.every, "models": summaries,
              "note": "Inference-only speed on sampled video. Detection counts are not accuracy. No ground-truth labels; no tracking/pose/UDP/RTH tested."}
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== YOLO11 vs YOLO26 ===")
    for model, data in summaries.items():
        print(f"{model:<12} {data['mean_ms']:>7.1f} ms/frame | {data['inference_fps']:>6.1f} infer FPS | counts={data['total_detections']}")
    print(f"\n[SAVED] {output / 'summary.json'}")
    print(f"[SAVED] {output / 'frame_results.csv'}")
    print("주의: 탐지 개수만으로 정확도 우열을 판단할 수 없어. 실제 정확도는 정답 라벨과 별도로 평가해야 해.")


if __name__ == "__main__":
    main()
