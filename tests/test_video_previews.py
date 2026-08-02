import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.generate_video_previews import flow_video_paths, manifest_is_current


class VideoPreviewGeneratorTest(unittest.TestCase):
    def test_flow_video_paths_follow_frontend_video_folder_rules(self):
        flow = {
            "videoFolder": "sample",
            "instructions": {"exampleVideoPath": "example.mp4"},
            "videos": [
                {"fileName": "first.mp4"},
                {"fileName": "dataset/second.mp4"},
            ],
        }

        self.assertEqual(
            flow_video_paths(flow),
            ["sample/first.mp4", "dataset/second.mp4", "sample/example.mp4"],
        )

    def test_manifest_current_check_includes_interval_and_source_version(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "video.mp4"
            source.write_bytes(b"video")
            preview = root / "preview"
            preview.mkdir()
            (preview / "sheet-0000.jpg").write_bytes(b"jpeg")
            stat = source.stat()
            manifest = {
                "videoPath": "dataset/video.mp4",
                "sourceSize": stat.st_size,
                "sourceMtimeNs": stat.st_mtime_ns,
                "intervalSeconds": 1,
                "thumbWidth": 320,
                "thumbHeight": 180,
                "columns": 10,
                "rows": 10,
                "sheets": ["sheet-0000.jpg"],
            }
            manifest_path = preview / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            self.assertTrue(
                manifest_is_current(
                    manifest_path,
                    source,
                    "dataset/video.mp4",
                    1,
                    320,
                    180,
                    10,
                    10,
                )
            )
            self.assertFalse(
                manifest_is_current(
                    manifest_path,
                    source,
                    "dataset/video.mp4",
                    5,
                    320,
                    180,
                    10,
                    10,
                )
            )


if __name__ == "__main__":
    unittest.main()
