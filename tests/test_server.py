import http.client
import threading
import unittest
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from human_eval_platform import server as server_module
from human_eval_platform.config import AppConfig, TrafficProtectionConfig, load_config
from human_eval_platform.server import (
    TokenBucketRateLimiter,
    TrafficGuard,
    create_handler,
    find_video_file,
    find_video_preview_asset,
    find_video_preview_manifest,
    find_video_text_file,
    if_range_allows_range,
    parse_range_header,
    request_is_not_modified,
    video_cache_control,
    video_preview_id,
)

server_module.ADMIN_PASSWORD = "test-admin-password"


class ServerTest(unittest.TestCase):
    def test_parse_range_header(self):
        self.assertEqual(parse_range_header("bytes=0-99", 1000), (0, 99))
        self.assertEqual(parse_range_header("bytes=100-", 1000), (100, 999))
        self.assertEqual(parse_range_header("bytes=-50", 1000), (950, 999))
        self.assertEqual(parse_range_header("items=0-10", 1000), (None, 0))

    def test_admin_password_check(self):
        handler_class = create_handler(None, None)
        handler = object.__new__(handler_class)
        sent_errors = []
        handler.headers = {}
        handler.send_error_json = lambda status, message: sent_errors.append((status, message))
        self.assertFalse(handler.has_admin_access())
        self.assertFalse(handler.require_admin())
        self.assertEqual(sent_errors, [(HTTPStatus.FORBIDDEN, "Administrator password required.")])

        handler.headers = {"X-Admin-Password": "test-admin-password"}
        self.assertTrue(handler.has_admin_access())
        self.assertTrue(handler.require_admin())

    def test_video_cache_and_range_responses(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            video_dir = root / "videos"
            static_dir = root / "static"
            video_dir.mkdir()
            static_dir.mkdir()
            (static_dir / "index.html").write_text("ok", encoding="utf-8")
            payload = b"0123456789abcdef"
            (video_dir / "sample.mp4").write_bytes(payload)
            config = AppConfig(
                host="127.0.0.1",
                port=0,
                database_path=root / "eval.db",
                flow_dir=root / "flows",
                video_dir=video_dir,
                export_dir=root / "exports",
                static_dir=static_dir,
                seed_flow_path=root / "seed.json",
                traffic_protection=TrafficProtectionConfig(enabled=False),
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(config, None))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def request(method="GET", headers=None):
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    server.server_address[1],
                    timeout=5,
                )
                connection.request(method, "/videos/sample.mp4", headers=headers or {})
                response = connection.getresponse()
                body = response.read()
                result = response.status, dict(response.getheaders()), body
                connection.close()
                return result

            try:
                status, headers, body = request()
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(body, payload)
                self.assertEqual(headers["Accept-Ranges"], "bytes")
                self.assertEqual(
                    headers["Cache-Control"],
                    "public, max-age=31536000, immutable",
                )
                self.assertIn("ETag", headers)
                self.assertIn("Last-Modified", headers)

                status, range_headers, body = request(headers={"Range": "bytes=2-5"})
                self.assertEqual(status, HTTPStatus.PARTIAL_CONTENT)
                self.assertEqual(body, b"2345")
                self.assertEqual(range_headers["Content-Range"], "bytes 2-5/16")
                self.assertEqual(range_headers["ETag"], headers["ETag"])
                self.assertEqual(range_headers["Cache-Control"], headers["Cache-Control"])

                status, cached_headers, body = request(
                    headers={"If-None-Match": headers["ETag"]}
                )
                self.assertEqual(status, HTTPStatus.NOT_MODIFIED)
                self.assertEqual(body, b"")
                self.assertEqual(cached_headers["ETag"], headers["ETag"])

                status, _, body = request(
                    headers={
                        "Range": "bytes=2-5",
                        "If-Range": '"outdated"',
                    }
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(body, payload)

                status, head_headers, body = request(method="HEAD")
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(body, b"")
                self.assertEqual(head_headers["Content-Length"], str(len(payload)))

                status, invalid_headers, body = request(headers={"Range": "bytes=99-100"})
                self.assertEqual(status, HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.assertEqual(body, b"")
                self.assertEqual(invalid_headers["Content-Range"], "bytes */16")
                self.assertEqual(invalid_headers["ETag"], headers["ETag"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_cache_condition_helpers(self):
        headers = {
            "If-None-Match": 'W/"old", W/"abc"',
            "If-Modified-Since": "Thu, 01 Jan 1970 00:00:00 GMT",
        }
        self.assertTrue(request_is_not_modified(headers, '"abc"', 1_000))
        self.assertFalse(if_range_allows_range('W/"abc"', '"abc"', 1_000))
        self.assertTrue(if_range_allows_range('"abc"', '"abc"', 1_000))
        self.assertEqual(video_cache_control(0), "public, max-age=0, must-revalidate")

    def test_request_and_video_concurrency_limits(self):
        limiter = TokenBucketRateLimiter()
        for _ in range(12):
            self.assertEqual(limiter.allow("video", "client", 60, now=100.0), (True, 0.0))
        allowed, retry_after = limiter.allow("video", "client", 60, now=100.0)
        self.assertFalse(allowed)
        self.assertAlmostEqual(retry_after, 1.0)
        self.assertEqual(limiter.allow("video", "client", 60, now=101.0), (True, 0.0))

        guard = TrafficGuard(
            TrafficProtectionConfig(
                max_video_connections_per_ip=1,
                max_video_connections_total=2,
            )
        )
        self.assertEqual(guard.acquire_video("one"), (True, None))
        self.assertEqual(guard.acquire_video("one"), (False, "per_ip"))
        self.assertEqual(guard.acquire_video("two"), (True, None))
        self.assertEqual(guard.acquire_video("three"), (False, "total"))
        guard.release_video("one")
        guard.release_video("two")

    def test_loads_video_cache_and_protection_config(self):
        with TemporaryDirectory() as temp:
            config_path = Path(temp) / "config.json"
            config_path.write_text(
                """
                {
                  "video_cache_max_age_seconds": 60,
                  "traffic_protection": {
                    "video_requests_per_minute_per_ip": 123,
                    "max_video_connections_total": 22,
                    "video_bandwidth_mib_per_second_total": 7.5
                  }
                }
                """,
                encoding="utf-8",
            )
            config = load_config(str(config_path))
            self.assertEqual(config.video_cache_max_age_seconds, 60)
            self.assertEqual(
                config.traffic_protection.video_requests_per_minute_per_ip,
                123,
            )
            self.assertEqual(config.traffic_protection.max_video_connections_total, 22)
            self.assertEqual(
                config.traffic_protection.video_bandwidth_mib_per_second_total,
                7.5,
            )

    def test_find_video_file_checks_extra_dirs(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary = root / "primary"
            extra = root / "extra"
            (extra / "dataset").mkdir(parents=True)
            video = extra / "dataset" / "full.mp4"
            video.write_bytes(b"video")
            config = AppConfig(
                host="127.0.0.1",
                port=0,
                database_path=root / "eval.db",
                flow_dir=root / "flows",
                video_dir=primary,
                export_dir=root / "exports",
                static_dir=root / "static",
                seed_flow_path=root / "seed.json",
                extra_video_dirs=(extra,),
            )

            self.assertEqual(find_video_file(config, "dataset/full.mp4"), video)
            self.assertIsNone(find_video_file(config, "../seed.json"))

    def test_video_preview_paths_are_stable_and_confined(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            preview_dir = root / "previews"
            preview_key = video_preview_id("dataset/full.mp4")
            asset_dir = preview_dir / preview_key
            asset_dir.mkdir(parents=True)
            manifest = asset_dir / "manifest.json"
            sheet = asset_dir / "sheet-0000.jpg"
            manifest.write_text("{}", encoding="utf-8")
            sheet.write_bytes(b"jpeg")
            config = AppConfig(
                host="127.0.0.1",
                port=0,
                database_path=root / "eval.db",
                flow_dir=root / "flows",
                video_dir=root / "videos",
                export_dir=root / "exports",
                static_dir=root / "static",
                seed_flow_path=root / "seed.json",
                video_preview_dir=preview_dir,
            )

            self.assertEqual(video_preview_id("/dataset/full.mp4"), preview_key)
            self.assertEqual(find_video_preview_manifest(config, "dataset/full.mp4"), manifest)
            self.assertEqual(find_video_preview_asset(config, f"{preview_key}/sheet-0000.jpg"), sheet)
            self.assertIsNone(find_video_preview_asset(config, "../outside.jpg"))
            self.assertIsNone(find_video_preview_asset(config, f"{preview_key}/manifest.json"))

    def test_find_video_text_file_by_shared_name_tokens(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary = root / "primary"
            extra = root / "extra"
            video_dir = extra / "dataset" / "01_sample_clip"
            video_dir.mkdir(parents=True)
            video = video_dir / "01_sample_clip__full_story_subtitle.mp4"
            text = video_dir / "01_sample_clip.txt"
            translation = video_dir / "01_sample_clip.translation.txt"
            video.write_bytes(b"video")
            text.write_text("Source story text", encoding="utf-8")
            translation.write_text("Translated story text", encoding="utf-8")
            config = AppConfig(
                host="127.0.0.1",
                port=0,
                database_path=root / "eval.db",
                flow_dir=root / "flows",
                video_dir=primary,
                export_dir=root / "exports",
                static_dir=root / "static",
                seed_flow_path=root / "seed.json",
                extra_video_dirs=(extra,),
            )

            self.assertEqual(
                find_video_text_file(
                    config,
                    "dataset/01_sample_clip/01_sample_clip__full_story_subtitle.mp4",
                ),
                text,
            )
            self.assertEqual(
                find_video_text_file(
                    config,
                    "dataset/01_sample_clip/01_sample_clip__full_story_subtitle.mp4",
                    language="translation",
                ),
                translation,
            )
            self.assertIsNone(find_video_text_file(config, "../seed.mp4"))


if __name__ == "__main__":
    unittest.main()
