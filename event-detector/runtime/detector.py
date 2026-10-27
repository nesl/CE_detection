#!/usr/bin/env python3
"""Run compiled complex events over bundled mobile YOLO and YAMNet results."""

from __future__ import annotations

import argparse
import json
import os
import time
from urllib import error, request

from .observations import labels_from_payload, observations_from_payload
from language import compile_event, load_event


class VisualizationClient:
    def __init__(self, base_url: str):
        self.url = base_url.rstrip("/") + "/api/update" if base_url else ""

    def update(self, data: dict) -> None:
        if not self.url:
            return
        body = json.dumps(data).encode()
        try:
            request.urlopen(request.Request(
                self.url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            ), timeout=0.25).close()
        except (error.URLError, TimeoutError):
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "definition",
        help="path to the YAML complex-event definition to parse and compile",
    )
    parser.add_argument("--host", default=os.getenv("MQTT_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
    parser.add_argument("--topic", default=os.getenv("MQTT_TOPIC", "ucla/ce_mobile"))
    parser.add_argument("--username", default=os.getenv("MQTT_USERNAME"))
    parser.add_argument("--password", default=os.getenv("MQTT_PASSWORD"))
    parser.add_argument("--min-confidence", type=float, default=float(os.getenv("CE_MIN_CONFIDENCE", "0")))
    parser.add_argument("--visualization-url", default=os.getenv("CE_VISUALIZATION_URL", "http://localhost:5000"))
    parser.add_argument("--session", default=os.getenv("CE_SESSION", "default"), help="persistence session name")
    parser.add_argument("--mongodb-uri", default=os.getenv("MONGODB_URI", "mongodb://localhost:27017"))
    parser.add_argument("--mongodb-database", default=os.getenv("MONGODB_DATABASE", "iobt_db"))
    parser.add_argument("--mongodb-collection", default=os.getenv("MONGODB_COLLECTION", "event_history"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    import paho.mqtt.client as mqtt

    detector = compile_event(load_event(args.definition))
    visualization = VisualizationClient(args.visualization_url)
    history = None
    if args.mongodb_uri:
        from services.persistence import MongoStore
        history = MongoStore(args.mongodb_uri, args.mongodb_database, args.mongodb_collection)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if args.username:
        client.username_pw_set(args.username, args.password)

    def on_connect(client, _userdata, _flags, reason_code, _properties):
        if reason_code != 0:
            raise RuntimeError(f"MQTT connection failed: {reason_code}")
        print(
            f"Compiled {detector.complex_event_name} from {args.definition}; "
            f"listening on {args.host}:{args.port}/{args.topic}"
        )
        if history is not None:
            history.save("detector_start", {
                "session": args.session,
                "event_name": detector.complex_event_name,
                "definition": args.definition,
                "topic": args.topic,
                "fsm": detector.status(),
            }, time.time())
        client.subscribe(args.topic)
        visualization.update({
            "session": args.session,
            "event_name": detector.complex_event_name,
            "definition": args.definition,
            "topic": args.topic,
            "completed": False,
            "fsm": detector.status(),
            "observations": [],
        })

    def on_message(_client, _userdata, message):
        try:
            observations = observations_from_payload(message.payload, args.min_confidence)
            completed = False
            for observation in observations:
                print(json.dumps({
                    "device": observation.device,
                    "modality": observation.modality,
                    "labels": observation.labels,
                }))
                if detector.observe(observation):
                    completed = True
                    print(json.dumps({"complex_event": detector.complex_event_name, "device": observation.device}))
            if observations:
                update = {
                    "session": args.session,
                    "event_name": detector.complex_event_name,
                    "definition": args.definition,
                    "topic": message.topic,
                    "raw_payload": message.payload.decode("utf-8", errors="replace"),
                    "completed": completed,
                    "fsm": detector.status(),
                    "observations": [
                        {
                            "device": item.device,
                            "timestamp": item.timestamp,
                            "modality": item.modality,
                            "labels": item.labels,
                            "scores": item.scores,
                        }
                        for item in observations
                    ],
                }
                if history is not None:
                    history.save("detector_update", update, time.time())
                visualization.update(update)
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            print(f"Ignored malformed message: {error}")

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.host, args.port, keepalive=60)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("Detector stopped by user.")
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
