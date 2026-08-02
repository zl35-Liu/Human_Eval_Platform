import json
import sqlite3
import tempfile
import unittest
import csv
import io
from pathlib import Path

from human_eval_platform.config import AppConfig
from human_eval_platform.store import EvaluationStore, ValidationError, answer_key, answers_equal


def sample_flow():
    return {
        "id": "test-flow",
        "title": "Test Workflow",
        "status": "published",
        "version": 1,
        "participantFields": [
            {"id": "participant_code", "label": "Participant", "required": True, "type": "text"}
        ],
        "instructions": {"title": "Instructions", "overview": "Read the instructions first."},
        "responseConfig": {
            "score": {"min": 1, "max": 5, "step": 1},
            "confidence": {"min": 1, "max": 5, "step": 1},
            "explanationRequired": True,
        },
        "videos": [{"id": "video-a", "title": "Video A", "fileName": "a.mp4"}],
        "dimensions": [
            {
                "id": "quality",
                "title": "Visual Quality",
                "questions": [{"id": "clarity", "prompt": "Is it clear?", "explanationRequired": True}],
            }
        ],
    }


class StoreTest(unittest.TestCase):
    def make_store(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        seed = root / "seed.json"
        seed.write_text(json.dumps(sample_flow()), encoding="utf-8")
        config = AppConfig(
            host="127.0.0.1",
            port=0,
            database_path=root / "eval.db",
            flow_dir=root / "flows",
            video_dir=root / "videos",
            export_dir=root / "exports",
            static_dir=root / "static",
            seed_flow_path=seed,
        )
        store = EvaluationStore(config)
        store.initialize()
        return store

    def tearDown(self):
        if hasattr(self, "tempdir"):
            self.tempdir.cleanup()

    def test_seed_flow_and_list_published(self):
        store = self.make_store()
        flows = store.list_flows()
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]["id"], "test-flow")

    def test_imports_flows_from_flow_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            flow_dir = root / "flows"
            flow_dir.mkdir()
            first_flow = sample_flow()
            second_flow = {**sample_flow(), "id": "second-flow", "title": "Second Test Workflow"}
            (flow_dir / "first.json").write_text(json.dumps(first_flow), encoding="utf-8")
            (flow_dir / "second.json").write_text(json.dumps(second_flow), encoding="utf-8")
            config = AppConfig(
                host="127.0.0.1",
                port=0,
                database_path=root / "eval.db",
                flow_dir=flow_dir,
                video_dir=root / "videos",
                export_dir=root / "exports",
                static_dir=root / "static",
                seed_flow_path=root / "missing.json",
            )
            store = EvaluationStore(config)
            store.initialize()
            flow_ids = {flow["id"] for flow in store.list_flows()}
            self.assertEqual(flow_ids, {"test-flow", "second-flow"})

    def test_submission_validation_and_csv_export(self):
        store = self.make_store()
        key = answer_key("video-a", "quality", "clarity")
        submission = store.create_submission(
            {
                "flow_id": "test-flow",
                "participant": {"participant_code": "P001"},
                "answers": {key: {"score": "4", "confidence": "5", "explanation": "The video is clear."}},
            }
        )
        self.assertEqual(submission["flow_id"], "test-flow")
        self.assertFalse(submission["is_hidden"])
        self.assertFalse(submission["is_pinned"])
        self.assertEqual(submission["video_order"], ["video-a"])
        self.assertEqual(submission["answer_reviews"], {})
        csv_text = store.export_submissions_csv("test-flow")
        self.assertIn("is_hidden", csv_text.splitlines()[0])
        self.assertIn("is_pinned", csv_text.splitlines()[0])
        self.assertIn("video_order_json", csv_text.splitlines()[0])
        self.assertIn("admin_comment", csv_text.splitlines()[0])
        self.assertIn("video-a", csv_text)
        self.assertIn("The video is clear.", csv_text)

    def test_text_references_are_normalized_and_export_full_text(self):
        store = self.make_store()
        key = answer_key("video-a", "quality", "clarity")
        quote = "At the critical moment, the messenger shouts and the group runs toward the gate."
        submission = store.create_submission(
            {
                "flow_id": "test-flow",
                "participant": {"participant_code": "P-reference"},
                "answers": {
                    key: {
                        "score": "3",
                        "confidence": "4",
                        "explanation": "Client text must not be authoritative",
                        "explanation_body": "Event is clear, but some action is obscured.",
                        "reference_placements": [
                            {"reference_id": "ref-01", "offset": 6}
                        ],
                        "references": [
                            {
                                "id": "ref-01",
                                "video_id": "video-a",
                                "language": "translation",
                                "source_key": "a.mp4::translation",
                                "start": 10,
                                "end": 10 + len(quote),
                                "source_length": 100,
                                "text": quote,
                                "prefix": "The group was resting beforehand.",
                                "suffix": "Hoofbeats follow shortly afterward.",
                            }
                        ],
                    }
                },
            }
        )
        answer = submission["answers"][key]
        self.assertEqual(answer["references"][0]["text"], quote)
        self.assertEqual(answer["references"][0]["source_length"], 100)
        self.assertEqual(
            answer["reference_placements"],
            [{"reference_id": "ref-01", "offset": 6}],
        )
        self.assertEqual(
            answer["explanation"],
            f"Event '''[10%]: {quote}'''is clear, but some action is obscured.",
        )
        csv_text = store.export_submissions_csv("test-flow")
        self.assertIn(
            f"Event '''[10%]: {quote}'''is clear, but some action is obscured.",
            csv_text,
        )
        self.assertNotIn("Client text must not be authoritative", csv_text)

    def test_text_reference_character_limits(self):
        store = self.make_store()
        key = answer_key("video-a", "quality", "clarity")

        def reference(index, text):
            start = (index - 1) * 600
            return {
                "id": f"ref-{index}",
                "video_id": "video-a",
                "language": "translation",
                "source_key": "a.mp4::translation",
                "start": start,
                "end": start + len(text),
                "source_length": 4000,
                "text": text,
                "prefix": "",
                "suffix": "",
            }

        boundary_references = [reference(index, "x" * 500) for index in range(1, 6)]
        accepted = store.create_submission(
            {
                "flow_id": "test-flow",
                "participant": {"participant_code": "P-boundary"},
                "answers": {
                    key: {
                        "score": "4",
                        "confidence": "4",
                        "explanation_body": "Boundary test",
                        "references": boundary_references,
                    }
                },
            }
        )
        self.assertEqual(len(accepted["answers"][key]["references"]), 5)

        with self.assertRaisesRegex(ValidationError, "at most 500 characters"):
            store.create_submission(
                {
                    "flow_id": "test-flow",
                    "participant": {"participant_code": "P-too-long"},
                    "answers": {
                        key: {
                            "score": "4",
                            "confidence": "4",
                            "explanation_body": "Single reference exceeds the limit",
                            "references": [reference(1, "x" * 501)],
                        }
                    },
                }
            )

        over_total_references = boundary_references + [reference(6, "x")]
        with self.assertRaisesRegex(ValidationError, "total at most 2500 characters"):
            store.create_submission(
                {
                    "flow_id": "test-flow",
                    "participant": {"participant_code": "P-total-too-long"},
                    "answers": {
                        key: {
                            "score": "4",
                            "confidence": "4",
                            "explanation_body": "Total references exceed the limit",
                            "references": over_total_references,
                        }
                    },
                }
            )

    def test_video_time_references_are_normalized_and_exported(self):
        store = self.make_store()
        key = answer_key("video-a", "quality", "clarity")
        submission = store.create_submission(
            {
                "flow_id": "test-flow",
                "participant": {"participant_code": "P-video-reference"},
                "answers": {
                    key: {
                        "score": "3",
                        "confidence": "4",
                        "explanation_body": " The outcome is unclear at this point.",
                        "reference_placements": [
                            {"reference_id": "ref-video-01", "offset": 0}
                        ],
                        "references": [
                            {
                                "id": "ref-video-01",
                                "type": "video_time",
                                "video_id": "video-a",
                                "time_seconds": 80,
                                "client_label": "Client label must not be stored",
                            }
                        ],
                    }
                },
            }
        )
        answer = submission["answers"][key]
        self.assertEqual(
            answer["references"],
            [
                {
                    "id": "ref-video-01",
                    "type": "video_time",
                    "video_id": "video-a",
                    "time_seconds": 80,
                }
            ],
        )
        self.assertEqual(
            answer["explanation"],
            "'''Video[1:20]''' The outcome is unclear at this point.",
        )
        self.assertIn(
            "'''Video[1:20]''' The outcome is unclear at this point.",
            store.export_submissions_csv("test-flow"),
        )
        self.assertNotIn(
            "Client label must not be stored",
            store.export_submissions_csv("test-flow"),
        )

    def test_video_time_reference_validation(self):
        store = self.make_store()
        key = answer_key("video-a", "quality", "clarity")

        def payload(references):
            return {
                "flow_id": "test-flow",
                "participant": {"participant_code": "P-invalid-video-reference"},
                "answers": {
                    key: {
                        "score": "3",
                        "confidence": "4",
                        "explanation_body": "Pending review",
                        "references": references,
                    }
                },
            }

        def reference(reference_id="ref-video-01", **overrides):
            value = {
                "id": reference_id,
                "type": "video_time",
                "video_id": "video-a",
                "time_seconds": 80,
            }
            value.update(overrides)
            return value

        invalid_cases = [
            (
                [reference(video_id="video-b")],
                "video_id must match the answer video",
            ),
            (
                [reference(time_seconds=-1)],
                "time_seconds must be a non-negative integer",
            ),
            (
                [reference(time_seconds=1.5)],
                "time_seconds must be a non-negative integer",
            ),
            (
                [reference(), reference("ref-video-02")],
                "duplicates an existing video time reference",
            ),
        ]
        for references, message in invalid_cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValidationError, message):
                    store.create_submission(payload(references))

    def test_structured_answer_comparison_ignores_legacy_explanation_layout(self):
        quote = "Complete quoted source"
        reference = {
            "id": "ref-01",
            "video_id": "video-a",
            "language": "translation",
            "source_key": "a.mp4::translation",
            "start": 3,
            "end": 3 + len(quote),
            "text": quote,
            "prefix": "",
            "suffix": "",
        }
        legacy = {
            "score": "3",
            "confidence": "4",
            "explanation_body": "Body",
            "references": [reference],
            "explanation": f"Reference 01: \"{quote}\"\n\nEvaluation Notes:\nBody",
        }
        inline = {
            **legacy,
            "reference_placements": [{"reference_id": "ref-01", "offset": 4}],
            "explanation": f"Body \"{quote}\"",
        }
        self.assertTrue(answers_equal(legacy, inline))

    def test_structured_answer_comparison_detects_video_reference_time_change(self):
        base = {
            "score": "3",
            "confidence": "4",
            "explanation_body": "Body",
            "references": [
                {
                    "id": "ref-video-01",
                    "type": "video_time",
                    "video_id": "video-a",
                    "time_seconds": 80,
                }
            ],
            "reference_placements": [{"reference_id": "ref-video-01", "offset": 0}],
            "explanation": "'''Video[1:20]'''Body",
        }
        changed = {
            **base,
            "references": [
                {
                    **base["references"][0],
                    "time_seconds": 81,
                }
            ],
            "explanation": "'''Video[1:21]'''Body",
        }
        self.assertFalse(answers_equal(base, changed))

    def test_submission_rejects_missing_required_answer(self):
        store = self.make_store()
        with self.assertRaises(ValidationError):
            store.create_submission(
                {
                    "flow_id": "test-flow",
                    "participant": {"participant_code": "P001"},
                    "answers": {},
                }
            )

    def test_cancelled_video_answers_are_not_required_or_saved(self):
        store = self.make_store()
        flow = sample_flow()
        flow["videos"] = [
            {"id": "video-a", "title": "Video A", "fileName": "a.mp4"},
            {
                "id": "video-cancelled",
                "title": "Cancelled",
                "fileName": "cancelled.mp4",
                "cancelled": True,
                "cancelMessage": "This video has been cancelled",
            },
        ]
        store.save_flow(flow, status="published")
        active_key = answer_key("video-a", "quality", "clarity")
        cancelled_key = answer_key("video-cancelled", "quality", "clarity")

        submission = store.create_submission(
            {
                "flow_id": "test-flow",
                "participant": {"participant_code": "P001"},
                "answers": {
                    active_key: {"score": "4", "confidence": "5", "explanation": "The video is clear."},
                    cancelled_key: {"score": "1", "confidence": "1", "explanation": "Should be ignored."},
                },
            }
        )

        self.assertIn(active_key, submission["answers"])
        self.assertNotIn(cancelled_key, submission["answers"])
        csv_text = store.export_submissions_csv("test-flow")
        self.assertIn("video-a", csv_text)
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        self.assertEqual([row["video_id"] for row in rows], ["video-a"])

    def test_participant_progress_reuses_existing_submission(self):
        store = self.make_store()
        key = answer_key("video-a", "quality", "clarity")
        participant = {"participant_code": "P001"}
        draft = store.save_submission_progress(
            {
                "flow_id": "test-flow",
                "participant": participant,
                "answers": {key: {"score": "", "confidence": "", "explanation": "Taking notes."}},
            }
        )
        self.assertEqual(draft["status"], "draft")

        submitted = store.create_submission(
            {
                "flow_id": "test-flow",
                "participant": participant,
                "answers": {key: {"score": "4", "confidence": "5", "explanation": "The video is clear."}},
            }
        )
        self.assertEqual(submitted["id"], draft["id"])
        self.assertEqual(submitted["status"], "submitted")

        revised = store.create_submission(
            {
                "flow_id": "test-flow",
                "participant": {"participant_code": "p001"},
                "answers": {key: {"score": "3", "confidence": "4", "explanation": "Revised after review."}},
            }
        )
        self.assertEqual(revised["id"], draft["id"])
        submissions = store.list_submissions("test-flow")
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0]["answers"][key]["score"], "3")

    def test_participant_video_order_is_created_once_and_reused(self):
        store = self.make_store()
        flow = sample_flow()
        flow["videos"] = [
            {"id": "video-a", "title": "Video A", "fileName": "a.mp4"},
            {"id": "video-b", "title": "Video B", "fileName": "b.mp4"},
            {"id": "video-c", "title": "Video C", "fileName": "c.mp4"},
        ]
        store.save_flow(flow, status="published")
        participant = {"participant_code": "P001"}
        first = store.get_or_create_participant_submission(
            {"flow_id": "test-flow", "participant": participant}
        )
        second = store.get_or_create_participant_submission(
            {"flow_id": "test-flow", "participant": participant}
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(set(first["video_order"]), {"video-a", "video-b", "video-c"})
        self.assertEqual(len(first["video_order"]), 3)
        self.assertEqual(second["video_order"], first["video_order"])

    def test_changed_answer_keys_merge_without_overwriting_other_answers(self):
        store = self.make_store()
        flow = sample_flow()
        flow["dimensions"][0]["questions"].append(
            {"id": "motion", "prompt": "Smooth?", "explanationRequired": True}
        )
        store.save_flow(flow, status="published")
        clarity = answer_key("video-a", "quality", "clarity")
        motion = answer_key("video-a", "quality", "motion")
        participant = {"participant_code": "P001"}
        initial = {
            clarity: {"score": "4", "confidence": "5", "explanation": "Clear."},
            motion: {"score": "3", "confidence": "4", "explanation": "Mostly smooth."},
        }
        store.save_submission_progress(
            {"flow_id": "test-flow", "participant": participant, "answers": initial}
        )
        revised = store.save_submission_progress(
            {
                "flow_id": "test-flow",
                "participant": participant,
                "answers": {
                    clarity: {"score": "2", "confidence": "5", "explanation": "Revised."}
                },
                "changed_answer_keys": [clarity],
            }
        )
        self.assertEqual(revised["answers"][clarity]["score"], "2")
        self.assertEqual(revised["answers"][motion], initial[motion])

    def test_admin_review_preserves_answer_until_user_revises_it(self):
        store = self.make_store()
        key = answer_key("video-a", "quality", "clarity")
        participant = {"participant_code": "P001"}
        original = {"score": "4", "confidence": "5", "explanation": "The video is clear."}
        submission = store.create_submission(
            {
                "flow_id": "test-flow",
                "participant": participant,
                "answers": {key: original},
            }
        )

        marked = store.mark_submission_answer_for_revision(submission["id"], key, "Add details about the occlusion.")
        self.assertEqual(marked["status"], "draft")
        self.assertEqual(marked["answers"][key], original)
        self.assertEqual(marked["answer_reviews"][key]["status"], "needs_revision")
        with self.assertRaises(ValidationError):
            store.create_submission(
                {
                    "flow_id": "test-flow",
                    "participant": participant,
                    "answers": {},
                    "changed_answer_keys": [],
                }
            )

        revised_answer = {"score": "3", "confidence": "5", "explanation": "Added details about the occlusion."}
        revised = store.save_submission_progress(
            {
                "flow_id": "test-flow",
                "participant": participant,
                "answers": {key: revised_answer},
                "changed_answer_keys": [key],
            }
        )
        self.assertEqual(revised["answers"][key], revised_answer)
        self.assertEqual(revised["answer_reviews"][key]["status"], "resolved")
        submitted = store.create_submission(
            {
                "flow_id": "test-flow",
                "participant": participant,
                "answers": {},
                "changed_answer_keys": [],
            }
        )
        self.assertEqual(submitted["status"], "submitted")
        csv_text = store.export_submissions_csv("test-flow")
        self.assertIn("Add details about the occlusion.", csv_text)
        self.assertIn("resolved", csv_text)

        reverted = store.save_submission_progress(
            {
                "flow_id": "test-flow",
                "participant": participant,
                "answers": {key: original},
                "changed_answer_keys": [key],
            }
        )
        self.assertEqual(reverted["answer_reviews"][key]["status"], "needs_revision")

    def test_admin_flags_pin_hide_and_export_keeps_hidden(self):
        store = self.make_store()
        key = answer_key("video-a", "quality", "clarity")
        first = store.create_submission(
            {
                "flow_id": "test-flow",
                "participant": {"participant_code": "P001"},
                "answers": {key: {"score": "4", "confidence": "5", "explanation": "First."}},
            }
        )
        second = store.create_submission(
            {
                "flow_id": "test-flow",
                "participant": {"participant_code": "P002"},
                "answers": {key: {"score": "3", "confidence": "4", "explanation": "Second."}},
            }
        )

        pinned = store.update_submission_admin_flags(first["id"], is_pinned=True)
        self.assertIsNotNone(pinned)
        self.assertTrue(pinned["is_pinned"])
        submissions = store.list_submissions("test-flow")
        self.assertEqual(submissions[0]["id"], first["id"])

        hidden = store.update_submission_admin_flags(first["id"], is_hidden=True)
        self.assertIsNotNone(hidden)
        self.assertTrue(hidden["is_hidden"])
        visible = store.list_submissions("test-flow")
        self.assertEqual([submission["id"] for submission in visible], [second["id"]])

        all_submissions = store.list_submissions("test-flow", include_hidden=True)
        self.assertIn(first["id"], {submission["id"] for submission in all_submissions})
        csv_text = store.export_submissions_csv("test-flow")
        self.assertIn(first["id"], csv_text)
        self.assertIn("First.", csv_text)

    def test_migrates_legacy_submission_table(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            seed = root / "seed.json"
            seed.write_text(json.dumps(sample_flow()), encoding="utf-8")
            database_path = root / "eval.db"
            with sqlite3.connect(database_path) as db:
                db.executescript(
                    """
                    CREATE TABLE flows (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        status TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        definition_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        published_at TEXT
                    );
                    CREATE TABLE submissions (
                        id TEXT PRIMARY KEY,
                        flow_id TEXT NOT NULL,
                        flow_version INTEGER NOT NULL,
                        participant_json TEXT NOT NULL,
                        answers_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    """
                )
                db.execute(
                    """
                    INSERT INTO flows
                    (id, title, status, version, definition_json, created_at, updated_at, published_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "test-flow",
                        "Test Workflow",
                        "published",
                        1,
                        json.dumps(sample_flow()),
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
                db.execute(
                    """
                    INSERT INTO submissions
                    (id, flow_id, flow_version, participant_json, answers_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-submission",
                        "test-flow",
                        1,
                        json.dumps({"participant_code": "P001"}),
                        json.dumps({}),
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
            config = AppConfig(
                host="127.0.0.1",
                port=0,
                database_path=database_path,
                flow_dir=root / "flows",
                video_dir=root / "videos",
                export_dir=root / "exports",
                static_dir=root / "static",
                seed_flow_path=seed,
            )
            store = EvaluationStore(config)
            store.initialize()
            submission = store.get_participant_submission("test-flow", {"participant_code": "p001"})
            self.assertIsNotNone(submission)
            self.assertEqual(submission["id"], "legacy-submission")
            self.assertEqual(submission["status"], "submitted")
            self.assertFalse(submission["is_hidden"])
            self.assertFalse(submission["is_pinned"])
            self.assertEqual(submission["video_order"], [])
            self.assertEqual(submission["answer_reviews"], {})


if __name__ == "__main__":
    unittest.main()
