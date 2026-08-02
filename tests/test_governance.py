import http.client
import json
import threading
import unittest
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from human_eval_platform import server as server_module
from human_eval_platform.config import (
    AppConfig,
    ParticipantGovernanceConfig,
    TrafficProtectionConfig,
)
from human_eval_platform.governance import (
    ParticipantAllowlist,
    ParticipantGovernance,
    canonicalize_participant_name,
    parse_allowlist_document,
)
from human_eval_platform.server import create_handler
from human_eval_platform.store import EvaluationStore

server_module.ADMIN_PASSWORD = "test-admin-password"


def sample_flow():
    return {
        "id": "test-flow",
        "title": "Test Workflow",
        "status": "published",
        "version": 1,
        "participantFields": [
            {
                "id": "participant_code",
                "label": "Participant identifier",
                "required": True,
                "type": "text",
            }
        ],
        "instructions": {},
        "videos": [{"id": "video-a", "title": "Video A", "fileName": "sample.mp4"}],
        "dimensions": [
            {
                "id": "dimension-a",
                "title": "Test Dimension",
                "questions": [{"id": "question-a", "prompt": "Test Criterion"}],
            }
        ],
    }


def allowlist_document(*names):
    return (
        "# Participant Allowlist\n\n"
        "## Identifiers\n\n"
        "```text\n"
        + "\n".join(names)
        + "\n```\n"
    )


class GovernanceTest(unittest.TestCase):
    def make_config(self, root, *, governance=None):
        seed = root / "flow.json"
        seed.write_text(json.dumps(sample_flow(), ensure_ascii=False), encoding="utf-8")
        allowlist = root / "allowlist.md"
        allowlist.write_text(
            allowlist_document("account-a", "account-demo"),
            encoding="utf-8",
        )
        static = root / "static"
        videos = root / "videos"
        static.mkdir()
        videos.mkdir()
        (static / "index.html").write_text("ok", encoding="utf-8")
        (videos / "sample.mp4").write_bytes(b"0123456789abcdef")
        return AppConfig(
            host="127.0.0.1",
            port=0,
            database_path=root / "eval.db",
            flow_dir=root / "flows",
            video_dir=videos,
            export_dir=root / "exports",
            static_dir=static,
            seed_flow_path=seed,
            participant_allowlist_path=allowlist,
            participant_governance=governance
            or ParticipantGovernanceConfig(enabled=True),
            traffic_protection=TrafficProtectionConfig(enabled=False),
        )

    def test_allowlist_parsing_normalization_and_hot_reload(self):
        parsed = parse_allowlist_document(
            allowlist_document("account-demo", "account-sample")
        )
        self.assertEqual(
            parsed[canonicalize_participant_name("account-demo")],
            "account-demo",
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_allowlist_document(
                allowlist_document("account-demo", "ACCOUNT-DEMO")
            )

        with TemporaryDirectory() as temp:
            path = Path(temp) / "allowlist.md"
            path.write_text(allowlist_document("account-a"), encoding="utf-8")
            allowlist = ParticipantAllowlist(path, reload_seconds=1)
            self.assertEqual(
                allowlist.is_allowed(" account-a "),
                (True, "account-a"),
            )
            original_hash = allowlist.status()["active_hash"]

            path.write_text(
                allowlist_document("account-a", "account-b"),
                encoding="utf-8",
            )
            allowlist.reload(force=True)
            self.assertTrue(allowlist.is_allowed("account-b")[0])
            self.assertNotEqual(allowlist.status()["active_hash"], original_hash)

            path.write_text("# Invalid allowlist", encoding="utf-8")
            allowlist.reload(force=True)
            self.assertTrue(allowlist.is_allowed("account-b")[0])
            self.assertFalse(allowlist.status()["healthy"])

    def test_multiplicative_policy_page_deduplication_and_session_revocation(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            governance_config = ParticipantGovernanceConfig(
                enabled=True,
                traffic_thresholds_gb=(1e-9, 2e-9),
                traffic_factors=(1, 0.75, 0),
                refresh_thresholds=(4, 8),
                refresh_factors=(1, 0.5, 0),
            )
            config = self.make_config(root, governance=governance_config)
            store = EvaluationStore(config)
            store.initialize()
            manager = ParticipantGovernance(config, store)
            token, principal = manager.create_session(
                principal_type="participant",
                flow_id="test-flow",
                participant_key="participant_code:account-alpha",
                canonical_participant_name="account-alpha",
                submission_id="submission-a",
            )
            manager.record_egress(principal, 1, "video")
            for index in range(4):
                manager.record_page_event(principal, f"page-{index}", "reload")
            inserted, usage = manager.record_page_event(principal, "page-3", "reload")
            self.assertFalse(inserted)
            self.assertEqual(usage["reload_count"], 4)
            self.assertAlmostEqual(usage["traffic_factor"], 0.75)
            self.assertAlmostEqual(usage["refresh_factor"], 0.5)
            self.assertAlmostEqual(usage["effective_factor"], 0.375)
            manager.flush()

            alerts = store.list_traffic_alerts()
            self.assertTrue(any(item["alert_type"] == "traffic" for item in alerts))
            self.assertTrue(any(item["alert_type"] == "refresh" for item in alerts))
            self.assertIsNotNone(manager.resolve_session(token, "participant"))
            manager.revoke_session(token)
            self.assertIsNone(manager.resolve_session(token, "participant"))

    def test_http_allowlist_refresh_block_autosave_and_admin_exemption(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.make_config(
                root,
                governance=ParticipantGovernanceConfig(
                    enabled=True,
                    refresh_thresholds=(2,),
                    refresh_factors=(1, 0),
                ),
            )
            store = EvaluationStore(config)
            store.initialize()
            manager = ParticipantGovernance(config, store)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                create_handler(config, store, manager),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def request(method, path, payload=None, headers=None):
                request_headers = dict(headers or {})
                body = None
                if payload is not None:
                    body = json.dumps(payload).encode("utf-8")
                    request_headers["Content-Type"] = "application/json"
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    server.server_address[1],
                    timeout=5,
                )
                connection.request(method, path, body=body, headers=request_headers)
                response = connection.getresponse()
                response_body = response.read()
                result = response.status, response.getheaders(), response_body
                connection.close()
                return result

            def json_body(body):
                return json.loads(body.decode("utf-8"))

            def cookie_from(headers, name):
                cookie = SimpleCookie()
                for key, value in headers:
                    if key.lower() == "set-cookie":
                        cookie.load(value)
                return cookie[name].value

            try:
                status, _, _ = request(
                    "POST",
                    "/api/participant-session",
                    {
                        "flow_id": "test-flow",
                        "participant": {"participant_code": "account-c"},
                    },
                )
                self.assertEqual(status, HTTPStatus.FORBIDDEN)
                self.assertEqual(store.list_submissions("test-flow"), [])

                config.participant_allowlist_path.write_text(
                    allowlist_document("account-a", "account-b", "account-c"),
                    encoding="utf-8",
                )
                manager.allowlist.reload(force=True)
                status, _, _ = request(
                    "POST",
                    "/api/participant-session",
                    {
                        "flow_id": "test-flow",
                        "participant": {"participant_code": "account-c"},
                    },
                )
                self.assertEqual(status, HTTPStatus.OK)

                status, headers, body = request(
                    "POST",
                    "/api/participant-session",
                    {
                        "flow_id": "test-flow",
                        "participant": {"participant_code": "account-a"},
                    },
                )
                self.assertEqual(status, HTTPStatus.OK)
                participant_token = cookie_from(headers, "hep_participant_session")
                participant_cookie = {
                    "Cookie": f"hep_participant_session={participant_token}"
                }
                submission = json_body(body)["submission"]

                status, _, _ = request("GET", "/videos/sample.mp4")
                self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
                status, headers, body = request(
                    "GET",
                    "/videos/sample.mp4",
                    headers={**participant_cookie, "Range": "bytes=2-5"},
                )
                self.assertEqual(status, HTTPStatus.PARTIAL_CONTENT)
                self.assertEqual(body, b"2345")
                self.assertEqual(dict(headers)["Cache-Control"], "private, max-age=31536000, immutable")

                for index in range(2):
                    status, _, body = request(
                        "POST",
                        "/api/usage/page-event",
                        {
                            "page_instance_id": f"page-{index}",
                            "event_type": "page_load",
                            "navigation_type": "reload",
                        },
                        participant_cookie,
                    )
                    self.assertEqual(status, HTTPStatus.OK)
                self.assertTrue(json_body(body)["usage"]["blocked"])

                status, _, _ = request(
                    "GET",
                    "/videos/sample.mp4",
                    headers=participant_cookie,
                )
                self.assertEqual(status, HTTPStatus.TOO_MANY_REQUESTS)

                status, _, body = request(
                    "POST",
                    "/api/submissions/draft",
                    {
                        "flow_id": "test-flow",
                        "participant": {"participant_code": "account-a"},
                        "answers": {},
                        "changed_answer_keys": [],
                    },
                    participant_cookie,
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(json_body(body)["submission"]["id"], submission["id"])

                status, headers, _ = request(
                    "GET",
                    "/api/admin/check",
                    headers={"X-Admin-Password": "test-admin-password"},
                )
                self.assertEqual(status, HTTPStatus.OK)
                admin_token = cookie_from(headers, "hep_admin_session")
                status, _, body = request(
                    "GET",
                    "/videos/sample.mp4",
                    headers={"Cookie": f"hep_admin_session={admin_token}"},
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(body, b"0123456789abcdef")
                status, _, body = request(
                    "GET",
                    "/api/admin/traffic/daily",
                    headers={"Cookie": f"hep_admin_session={admin_token}"},
                )
                self.assertEqual(status, HTTPStatus.OK)
                admin_data = json_body(body)
                self.assertTrue(
                    any(
                        item["participant_name"] == "account-a"
                        for item in admin_data["usage"]
                    )
                )
                self.assertTrue(
                    any(item["alert_type"] == "refresh" for item in admin_data["alerts"])
                )
            finally:
                manager.flush()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
