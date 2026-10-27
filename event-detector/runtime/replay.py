#!/usr/bin/env python3
"""Run the phone vision model over synchronized videos and publish MQTT detections."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import heapq
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Callable, Iterable, Iterator
import zipfile

import cv2
import numpy as np


VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}


@dataclass(frozen=True)
class Detection:
    label: str
    confidence: float
    color: str


@dataclass(frozen=True)
class VideoFrame:
    device: str
    timestamp: float
    image: np.ndarray


@dataclass(frozen=True)
class AudioFrame:
    device: str
    timestamp: float
    samples: np.ndarray


def discover_videos(directory: str | Path) -> list[Path]:
    root = Path(directory)
    if not root.is_dir():
        raise ValueError(f"recording directory does not exist: {root}")
    videos = sorted(path for path in root.rglob("*") if path.suffix.lower() in VIDEO_SUFFIXES)
    if not videos:
        supported = ", ".join(sorted(VIDEO_SUFFIXES))
        raise ValueError(f"no videos found in {root} (supported: {supported})")
    stems = [path.stem for path in videos]
    duplicates = {stem for stem in stems if stems.count(stem) > 1}
    if duplicates:
        raise ValueError(f"video filenames must be unique; duplicate device IDs: {sorted(duplicates)}")
    return videos


def iter_video(path: Path, sample_fps: float) -> Iterator[VideoFrame]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise ValueError(f"cannot open video: {path}")
    source_fps = capture.get(cv2.CAP_PROP_FPS)
    if not source_fps or source_fps <= 0:
        source_fps = sample_fps
    next_sample = 0.0
    frame_index = 0
    try:
        while True:
            ok, image = capture.read()
            if not ok:
                break
            timestamp = frame_index / source_fps
            frame_index += 1
            if timestamp + 1e-9 < next_sample:
                continue
            next_sample += 1.0 / sample_fps
            yield VideoFrame(path.stem, timestamp, image)
    finally:
        capture.release()


def synchronized_frames(paths: list[Path], sample_fps: float) -> Iterator[VideoFrame]:
    streams = [iter(iter_video(path, sample_fps)) for path in paths]
    queue: list[tuple[float, int, VideoFrame]] = []
    for index, stream in enumerate(streams):
        frame = next(stream, None)
        if frame is not None:
            heapq.heappush(queue, (frame.timestamp, index, frame))
    while queue:
        _, index, frame = heapq.heappop(queue)
        yield frame
        following = next(streams[index], None)
        if following is not None:
            heapq.heappush(queue, (following.timestamp, index, following))


def iter_audio(path: Path) -> Iterator[AudioFrame]:
    """Decode overlapping 0.975-second mono windows from a video's audio track."""
    command = [
        "ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
        "-f", "f32le", "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert process.stdout is not None
    window_size, hop_size = 15_600, 7_800
    buffered = np.empty(0, dtype=np.float32)
    index = 0
    try:
        while True:
            chunk = process.stdout.read(hop_size * 4)
            if not chunk:
                break
            buffered = np.concatenate((buffered, np.frombuffer(chunk, dtype="<f4")))
            while len(buffered) >= window_size:
                yield AudioFrame(path.stem, index * hop_size / 16_000, buffered[:window_size].copy())
                buffered = buffered[hop_size:]
                index += 1
    finally:
        process.stdout.close()
        process.wait()


def synchronized_events(paths: list[Path], sample_fps: float) -> Iterator[VideoFrame | AudioFrame]:
    streams: list[Iterator[VideoFrame | AudioFrame]] = []
    for path in paths:
        streams.extend((iter(iter_video(path, sample_fps)), iter(iter_audio(path))))
    queue: list[tuple[float, int, VideoFrame | AudioFrame]] = []
    for index, stream in enumerate(streams):
        event = next(stream, None)
        if event is not None:
            heapq.heappush(queue, (event.timestamp, index, event))
    while queue:
        _, index, event = heapq.heappop(queue)
        yield event
        following = next(streams[index], None)
        if following is not None:
            heapq.heappush(queue, (following.timestamp, index, following))


class VisionModel:
    def __init__(self, model_path: str | Path, labels_path: str | Path, confidence: float = 0.3):
        from ai_edge_litert.interpreter import Interpreter

        self.interpreter = Interpreter(model_path=str(model_path), num_threads=4)
        self.interpreter.allocate_tensors()
        self.input = self.interpreter.get_input_details()[0]
        self.output = self.interpreter.get_output_details()[0]
        shape = self.input["shape"]
        self.height, self.width = (int(shape[1]), int(shape[2])) if shape[1] != 3 else (int(shape[2]), int(shape[3]))
        self.labels = Path(labels_path).read_text(encoding="utf-8").splitlines()
        self.confidence = confidence

    def detect(self, image: np.ndarray) -> list[Detection]:
        resized = cv2.resize(image, (self.width, self.height), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        self.interpreter.set_tensor(self.input["index"], rgb[np.newaxis, ...])
        self.interpreter.invoke()
        output = np.squeeze(self.interpreter.get_tensor(self.output["index"]))
        if output.shape[0] < output.shape[1]:
            output = output.T
        candidates = []
        for row in output:
            scores = row[4:]
            class_id = int(np.argmax(scores))
            score = float(scores[class_id])
            if score < self.confidence or class_id >= len(self.labels):
                continue
            cx, cy, width, height = map(float, row[:4])
            x1, y1, x2, y2 = cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2
            if min(x1, y1) < 0 or max(x2, y2) > 1:
                continue
            candidates.append((score, class_id, x1, y1, x2, y2))
        kept = []
        for candidate in sorted(candidates, reverse=True):
            if all(_iou(candidate, prior) < 0.5 for prior in kept):
                kept.append(candidate)
        return [Detection(self.labels[item[1]], item[0], _dominant_color(image, *item[2:])) for item in kept]


class AudioModel:
    def __init__(self, model_path: str | Path, confidence: float = 0.3, max_results: int = 3):
        from ai_edge_litert.interpreter import Interpreter

        self.interpreter = Interpreter(model_path=str(model_path), num_threads=4)
        self.interpreter.allocate_tensors()
        self.input = self.interpreter.get_input_details()[0]
        self.output = self.interpreter.get_output_details()[0]
        with zipfile.ZipFile(model_path) as archive:
            self.labels = archive.read("yamnet_label_list.txt").decode().splitlines()
        self.confidence = confidence
        self.max_results = max_results

    def classify(self, samples: np.ndarray) -> list[tuple[str, float]]:
        self.interpreter.set_tensor(self.input["index"], samples.astype(np.float32, copy=False))
        self.interpreter.invoke()
        scores = np.squeeze(self.interpreter.get_tensor(self.output["index"]))
        ranked = np.argsort(scores)[::-1][:self.max_results]
        return [(self.labels[index], float(scores[index])) for index in ranked if scores[index] >= self.confidence]


def _iou(left: tuple, right: tuple) -> float:
    x1, y1 = max(left[2], right[2]), max(left[3], right[3])
    x2, y2 = min(left[4], right[4]), min(left[5], right[5])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    left_area = (left[4] - left[2]) * (left[5] - left[3])
    right_area = (right[4] - right[2]) * (right[5] - right[3])
    return intersection / max(left_area + right_area - intersection, 1e-9)


def _dominant_color(image: np.ndarray, x1: float, y1: float, x2: float, y2: float) -> str:
    height, width = image.shape[:2]
    crop = image[max(0, int(y1 * height)):min(height, int(y2 * height)), max(0, int(x1 * width)):min(width, int(x2 * width))]
    if crop.size == 0:
        return "unknown"
    hue, saturation, value = cv2.split(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV))
    valid = (saturation > 60) & (value > 45)
    if not np.any(valid):
        return "unknown"
    h = float(np.median(hue[valid]))
    if h < 10 or h >= 170: return "red"
    if h < 25: return "orange"
    if h < 38: return "yellow"
    if h < 85: return "green"
    if h < 130: return "blue"
    if h < 155: return "purple"
    return "red"


def payload_for(frame: VideoFrame, detections: list[Detection], epoch: float) -> bytes:
    timestamp_ms = int(round((epoch + frame.timestamp) * 1000))
    boxes = [{"clsName": item.label, "cnf": item.confidence, "color": item.color} for item in detections]
    body = {
        "dev_id": frame.device,
        "detectionResults": [{"dev_id": frame.device, "timestamp": timestamp_ms, "bboxes": boxes}],
        "audioResults": [],
    }
    return ("detection::" + json.dumps(body, separators=(",", ":"))).encode()


def audio_payload_for(frame: AudioFrame, events: list[tuple[str, float]], epoch: float) -> bytes:
    timestamp_ms = int(round((epoch + frame.timestamp) * 1000))
    body = {
        "dev_id": frame.device,
        "detectionResults": [],
        "audioResults": [{
            "timestamp": timestamp_ms,
            "events": [{"first": label, "second": score} for label, score in events],
        }],
    }
    return ("detection::" + json.dumps(body, separators=(",", ":"))).encode()


def replay_videos(
    directory: str | Path, model: VisionModel, publish: Callable[[bytes], None], *,
    audio_model: AudioModel | None = None,
    speed: float = 1.0, sample_fps: float = 2.0, wait: bool = True,
    clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
) -> int:
    if speed <= 0 or sample_fps <= 0:
        raise ValueError("speed and sample FPS must be positive")
    paths = discover_videos(directory)
    started, epoch, count = clock(), time.time(), 0
    for frame in synchronized_events(paths, sample_fps):
        if wait:
            remaining = frame.timestamp / speed - (clock() - started)
            if remaining > 0:
                sleep(remaining)
        if isinstance(frame, VideoFrame):
            detections = model.detect(frame.image)
            publish(payload_for(frame, detections, epoch))
            labels = [item.label for item in detections]
            modality = "vision"
        else:
            events = audio_model.classify(frame.samples) if audio_model else []
            publish(audio_payload_for(frame, events, epoch))
            labels = [item[0] for item in events]
            modality = "audio"
        emit(f"{frame.timestamp:8.3f}s  {frame.device} {modality}: " + (", ".join(labels) or "no detections"))
        count += 1
    return count


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", help="folder containing synchronized video files")
    parser.add_argument("--host", default=os.getenv("MQTT_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
    parser.add_argument("--topic", default=os.getenv("MQTT_TOPIC", "ucla/ce_mobile"))
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--sample-fps", type=float, default=2.0)
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--model", default=os.getenv("VISION_MODEL", "/app/models/model.tflite"))
    parser.add_argument("--labels", default=os.getenv("VISION_LABELS", "/app/models/labels.txt"))
    parser.add_argument("--audio-model", default=os.getenv("AUDIO_MODEL", "/app/models/yamnet.tflite"))
    args = parser.parse_args(argv)

    import paho.mqtt.client as mqtt
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect(args.host, args.port, keepalive=60)
    client.loop_start()
    try:
        model = VisionModel(args.model, args.labels)
        audio_model = AudioModel(args.audio_model)
        def publish(payload: bytes) -> None:
            result = client.publish(args.topic, payload, qos=1)
            result.wait_for_publish(timeout=30)
        count = replay_videos(args.recording, model, publish, audio_model=audio_model, speed=args.speed, sample_fps=args.sample_fps, wait=not args.no_wait)
        print(f"Published {count} synchronized media observations to {args.host}:{args.port}/{args.topic}")
        return 0
    finally:
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    raise SystemExit(main())
