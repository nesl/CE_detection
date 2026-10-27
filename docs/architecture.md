# Architecture

The system accepts detections from either live Android phones or recorded
videos. Both sources publish the same observation format through MQTT, so the
complex-event detector does not need to know which source is being used.

```text
                         selected CE YAML
                               │
                               v
live Android phones ─┐     CE detector ──> MongoDB
  YOLO + YAMNet      ├─> MQTT     │
                     │            └──────> visualization
recording folder ────┘
  video + audio
  YOLO + YAMNet
```

## Live operation

Each Android phone runs YOLO on camera frames and YAMNet on microphone audio.
It publishes detections to MQTT using its configured device ID. The detector
compares those observations with the selected CE definition.

Start the receiver with:

```bash
./run ce3
```

No replay process is needed when phones are providing the observations.

## Replay operation

A recording is a folder containing one or more videos:

```text
convoy-recording/
├── outside_phone.mp4
├── building_phone.mp4
└── convoy_phone.mp4
```

The replay component searches the folder and its subfolders for videos. The
filename becomes the device ID, so `outside_phone.mp4` acts as the device
`outside_phone`. These names must match the devices declared by the selected CE
YAML.

All videos are assumed to begin at the same instant. Replay merges their frames
and embedded audio onto one clock, runs YOLO and YAMNet, and publishes the
results through MQTT at the recorded speed. Multiple files therefore behave
like multiple synchronized phones.

Run replay in a second terminal:

```bash
./run replay /path/to/convoy-recording
```

There is no replay manifest, CE-specific replay alias, NetWaggle dependency, or
alternate direct path to the detector.

## Detection and storage

The detector loads one CE YAML, converts incoming MQTT payloads into common
vision/audio observations, and advances the CE state machine. Each update and
the resulting state are saved in MongoDB and sent to the browser visualization.
Live and replayed observations are handled identically after they enter MQTT.

## Core files

Only these files are central to the runtime architecture:

- `event-detector/runtime/replay.py` discovers and synchronizes videos, runs
  YOLO/YAMNet, and publishes replay observations.
- `event-detector/runtime/observations.py` converts phone-format MQTT messages
  into the detector's common observation objects.
- `event-detector/runtime/detector.py` loads the selected CE and consumes MQTT
  observations; `fsm.py` contains its state machine.
- `ce_definitions/*.yaml` defines the devices, detection triggers, order, and
  timing for each complex event.
- `docker-compose.yml` connects MQTT, the detector, MongoDB, and visualization;
  the root `run` script provides the short user commands.

The architecture image used by the README is `docs/architecture.png`. It shows
both live phones and synchronized video replay; this document provides the
operational details behind that figure.
