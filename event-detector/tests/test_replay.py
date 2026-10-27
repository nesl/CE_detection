import json
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

from runtime.observations import observations_from_payload
from runtime.replay import Detection, VideoFrame, discover_videos, payload_for, synchronized_frames


class ReplayTests(unittest.TestCase):
    def test_discovers_supported_video_files_recursively(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            (root / "outside_phone.mp4").touch()
            (root / "nested" / "building_phone.MOV").touch()
            (root / "ignore.txt").touch()
            self.assertEqual(
                {path.stem for path in discover_videos(root)},
                {"outside_phone", "building_phone"},
            )

    def test_payload_uses_filename_device_and_mobile_format(self):
        frame = VideoFrame("outside_phone", 1.25, np.zeros((2, 2, 3), dtype=np.uint8))
        payload = payload_for(frame, [Detection("vehicle", 0.9, "red")], 100.0)
        observations = observations_from_payload(payload)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].device, "outside_phone")
        self.assertEqual(observations[0].timestamp, 101.25)
        self.assertEqual(observations[0].labels, ("vehicle",))
        self.assertEqual(observations[0].colors, ("red",))

    def test_videos_share_one_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for name in ("a", "b"):
                path = root / f"{name}.avi"
                writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 2, (8, 8))
                for _ in range(3):
                    writer.write(np.zeros((8, 8, 3), dtype=np.uint8))
                writer.release()
                paths.append(path)
            frames = list(synchronized_frames(paths, sample_fps=2))
            self.assertEqual([(item.device, item.timestamp) for item in frames], [
                ("a", 0.0), ("b", 0.0), ("a", 0.5), ("b", 0.5),
                ("a", 1.0), ("b", 1.0),
            ])


if __name__ == "__main__":
    unittest.main()
