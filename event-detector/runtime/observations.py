"""Convert Android MQTT payloads into detector observations."""

from __future__ import annotations

import json

from .fsm import Observation


def _pair(pair) -> tuple[str, float] | None:
    if isinstance(pair, dict):
        label, confidence = pair.get("first"), pair.get("second", 0)
    elif isinstance(pair, list) and pair:
        label, confidence = pair[0], pair[1] if len(pair) > 1 else 0
    else:
        return None
    if not isinstance(label, str):
        return None
    return label, float(confidence)


def observations_from_payload(payload: bytes, min_confidence: float = 0) -> list[Observation]:
    text = payload.decode("utf-8")
    if not text.startswith("detection::"):
        return []
    bundle = json.loads(text[len("detection::"):])
    device = str(bundle.get("dev_id", "unknown"))
    observations: list[Observation] = []

    for result in bundle.get("detectionResults", []):
        labels, scores, colors = [], [], []
        for box in result.get("bboxes", []):
            label = box.get("clsName")
            if isinstance(label, str) and float(box.get("cnf", 1)) >= min_confidence:
                labels.append(label)
                scores.append(float(box.get("cnf", 1)))
                if isinstance(box.get("color"), str):
                    colors.append(box["color"])
        observations.append(Observation(
            device=str(result.get("dev_id", device)),
            timestamp=float(result["timestamp"]) / 1000,
            modality="vision", labels=tuple(labels), scores=tuple(scores),
            colors=tuple(colors),
        ))

    for result in bundle.get("audioResults", []):
        labels, scores = [], []
        for raw_event in result.get("events", []):
            event = _pair(raw_event)
            if event is not None and event[1] >= min_confidence:
                labels.append(event[0])
                scores.append(event[1])
        observations.append(Observation(
            device=device, timestamp=float(result["timestamp"]) / 1000,
            modality="audio", labels=tuple(labels), scores=tuple(scores),
        ))
    return sorted(observations, key=lambda observation: observation.timestamp)


def labels_from_payload(payload: bytes) -> tuple[str, list[str]]:
    observations = observations_from_payload(payload)
    if not observations:
        return "unknown", []
    return observations[0].device, [label for item in observations for label in item.labels]
