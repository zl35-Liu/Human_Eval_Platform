from __future__ import annotations

import csv
import io
import json
import os
import random
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .config import AppConfig


JsonObject = Dict[str, Any]
TEXT_REFERENCE_MAX_CHARS = 500
TEXT_REFERENCE_TOTAL_MAX_CHARS = 2500


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ValidationError(ValueError):
    pass


class EvaluationStore:
    def __init__(self, config: AppConfig):
        self.config = config
        self.config.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.flow_dir.mkdir(parents=True, exist_ok=True)
        self.config.video_dir.mkdir(parents=True, exist_ok=True)
        self.config.export_dir.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.config.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS flows (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('draft', 'published')),
                    version INTEGER NOT NULL,
                    definition_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    published_at TEXT
                );

                CREATE TABLE IF NOT EXISTS submissions (
                    id TEXT PRIMARY KEY,
                    flow_id TEXT NOT NULL,
                    flow_version INTEGER NOT NULL,
                    participant_key TEXT,
                    participant_json TEXT NOT NULL,
                    answers_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'submitted',
                    is_hidden INTEGER NOT NULL DEFAULT 0,
                    is_pinned INTEGER NOT NULL DEFAULT 0,
                    video_order_json TEXT NOT NULL DEFAULT '[]',
                    answer_reviews_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(flow_id) REFERENCES flows(id)
                );

                CREATE TABLE IF NOT EXISTS participant_sessions (
                    id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    principal_type TEXT NOT NULL
                        CHECK(principal_type IN ('participant', 'admin')),
                    flow_id TEXT NOT NULL DEFAULT '',
                    participant_key TEXT NOT NULL DEFAULT '',
                    canonical_participant_name TEXT NOT NULL DEFAULT '',
                    submission_id TEXT NOT NULL DEFAULT '',
                    allowlist_hash_at_issue TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    revoked_at TEXT,
                    last_ip_hash TEXT NOT NULL DEFAULT '',
                    user_agent_hash TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS daily_traffic_usage (
                    flow_id TEXT NOT NULL,
                    participant_key TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    egress_bytes INTEGER NOT NULL DEFAULT 0,
                    video_bytes INTEGER NOT NULL DEFAULT 0,
                    preview_bytes INTEGER NOT NULL DEFAULT 0,
                    text_bytes INTEGER NOT NULL DEFAULT 0,
                    api_static_bytes INTEGER NOT NULL DEFAULT 0,
                    video_request_count INTEGER NOT NULL DEFAULT 0,
                    range_request_count INTEGER NOT NULL DEFAULT 0,
                    rejected_request_count INTEGER NOT NULL DEFAULT 0,
                    document_load_count INTEGER NOT NULL DEFAULT 0,
                    reported_reload_count INTEGER NOT NULL DEFAULT 0,
                    current_traffic_tier TEXT NOT NULL DEFAULT '0',
                    current_refresh_tier TEXT NOT NULL DEFAULT '0',
                    effective_factor REAL NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY(flow_id, participant_key, local_date)
                );

                CREATE TABLE IF NOT EXISTS traffic_page_events (
                    page_instance_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    flow_id TEXT NOT NULL,
                    participant_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    navigation_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    local_date TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS participant_access_policy (
                    flow_id TEXT NOT NULL,
                    participant_key TEXT NOT NULL,
                    manual_factor REAL NOT NULL DEFAULT 1,
                    manual_blocked_until TEXT,
                    manual_block_reason TEXT NOT NULL DEFAULT '',
                    created_by_admin TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(flow_id, participant_key)
                );

                CREATE TABLE IF NOT EXISTS traffic_alerts (
                    id TEXT PRIMARY KEY,
                    flow_id TEXT NOT NULL,
                    participant_key TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    observed_value INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    acknowledged_by TEXT NOT NULL DEFAULT '',
                    UNIQUE(flow_id, participant_key, local_date, alert_type, tier)
                );
                """
            )
            self._migrate_submissions_table(db)
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_submissions_flow_participant
                    ON submissions(flow_id, participant_key)
                """
            )
            db.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_participant_sessions_expires
                    ON participant_sessions(expires_at);
                CREATE INDEX IF NOT EXISTS idx_daily_traffic_usage_date
                    ON daily_traffic_usage(local_date, egress_bytes DESC);
                CREATE INDEX IF NOT EXISTS idx_traffic_page_events_date
                    ON traffic_page_events(local_date);
                CREATE INDEX IF NOT EXISTS idx_traffic_alerts_date
                    ON traffic_alerts(local_date, acknowledged_at);
                """
            )
        self.seed_flows_from_files()

    def _migrate_submissions_table(self, db: sqlite3.Connection) -> None:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(submissions)")}
        if "participant_key" not in columns:
            db.execute("ALTER TABLE submissions ADD COLUMN participant_key TEXT")
        if "status" not in columns:
            db.execute("ALTER TABLE submissions ADD COLUMN status TEXT")
        if "updated_at" not in columns:
            db.execute("ALTER TABLE submissions ADD COLUMN updated_at TEXT")
        if "is_hidden" not in columns:
            db.execute("ALTER TABLE submissions ADD COLUMN is_hidden INTEGER NOT NULL DEFAULT 0")
        if "is_pinned" not in columns:
            db.execute("ALTER TABLE submissions ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0")
        if "video_order_json" not in columns:
            db.execute("ALTER TABLE submissions ADD COLUMN video_order_json TEXT NOT NULL DEFAULT '[]'")
        if "answer_reviews_json" not in columns:
            db.execute("ALTER TABLE submissions ADD COLUMN answer_reviews_json TEXT NOT NULL DEFAULT '{}'")

        db.execute("UPDATE submissions SET status = 'submitted' WHERE status IS NULL OR status = ''")
        db.execute("UPDATE submissions SET updated_at = created_at WHERE updated_at IS NULL OR updated_at = ''")
        db.execute("UPDATE submissions SET is_hidden = 0 WHERE is_hidden IS NULL")
        db.execute("UPDATE submissions SET is_pinned = 0 WHERE is_pinned IS NULL")
        db.execute("UPDATE submissions SET video_order_json = '[]' WHERE video_order_json IS NULL OR video_order_json = ''")
        db.execute(
            "UPDATE submissions SET answer_reviews_json = '{}' "
            "WHERE answer_reviews_json IS NULL OR answer_reviews_json = ''"
        )

        rows = db.execute(
            """
            SELECT id, flow_id, participant_json
            FROM submissions
            WHERE participant_key IS NULL OR participant_key = ''
            """
        ).fetchall()
        for row in rows:
            flow_row = db.execute("SELECT definition_json FROM flows WHERE id = ?", (row["flow_id"],)).fetchone()
            if not flow_row:
                continue
            try:
                flow = json.loads(str(flow_row["definition_json"]))
                participant = json.loads(str(row["participant_json"]))
                participant_key = participant_key_for_flow(flow, participant)
            except (json.JSONDecodeError, ValidationError, TypeError):
                participant_key = f"legacy:{row['id']}"
            db.execute("UPDATE submissions SET participant_key = ? WHERE id = ?", (participant_key, row["id"]))

    def seed_flows_from_files(self) -> None:
        flow_paths = list(self.config.flow_dir.glob("*.json"))
        if self.config.seed_flow_path.exists() and self.config.seed_flow_path not in flow_paths:
            flow_paths.append(self.config.seed_flow_path)

        overwrite = os.environ.get("HEP_IMPORT_OVERWRITE") == "1"
        for path in sorted(set(flow_paths)):
            flow = json.loads(path.read_text(encoding="utf-8"))
            flow_id = str(flow.get("id", "")).strip()
            if not flow_id:
                raise ValidationError(f"Workflow file must contain an id: {path}")
            with self.connect() as db:
                row = db.execute("SELECT id FROM flows WHERE id = ?", (flow_id,)).fetchone()
            if row and not overwrite:
                continue
            self.save_flow(flow, status=str(flow.get("status", "published")))

    def list_flows(self, include_drafts: bool = False) -> list[JsonObject]:
        query = "SELECT * FROM flows"
        params: tuple[Any, ...] = ()
        if not include_drafts:
            query += " WHERE status = ?"
            params = ("published",)
        query += " ORDER BY updated_at DESC"
        with self.connect() as db:
            return [self._row_to_flow(row) for row in db.execute(query, params)]

    def get_flow(self, flow_id: str, include_draft: bool = False) -> JsonObject | None:
        query = "SELECT * FROM flows WHERE id = ?"
        params: tuple[Any, ...] = (flow_id,)
        if not include_draft:
            query += " AND status = ?"
            params = (flow_id, "published")
        with self.connect() as db:
            row = db.execute(query, params).fetchone()
        return self._row_to_flow(row) if row else None

    def save_flow(self, flow: JsonObject, status: str = "draft") -> JsonObject:
        self.validate_flow(flow)
        flow_id = str(flow["id"])
        title = str(flow["title"])
        now = utc_now()
        if status not in {"draft", "published"}:
            raise ValidationError("Workflow status must be draft or published")

        with self.connect() as db:
            existing = db.execute("SELECT version, status FROM flows WHERE id = ?", (flow_id,)).fetchone()
            version = 1
            created_at = now
            published_at = now if status == "published" else None
            if existing:
                version = int(existing["version"])
                created = db.execute("SELECT created_at, published_at FROM flows WHERE id = ?", (flow_id,)).fetchone()
                created_at = str(created["created_at"])
                published_at = str(created["published_at"]) if created["published_at"] else None
                if status == "published":
                    version += 1
                    published_at = now

            normalized = {**flow, "status": status, "version": version}
            db.execute(
                """
                INSERT INTO flows (id, title, status, version, definition_json, created_at, updated_at, published_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    status = excluded.status,
                    version = excluded.version,
                    definition_json = excluded.definition_json,
                    updated_at = excluded.updated_at,
                    published_at = excluded.published_at
                """,
                (
                    flow_id,
                    title,
                    status,
                    version,
                    json.dumps(normalized, ensure_ascii=False),
                    created_at,
                    now,
                    published_at,
                ),
            )
        saved = self.get_flow(flow_id, include_draft=True)
        if saved is None:
            raise RuntimeError("Saved workflow could not be read")
        return saved

    def publish_flow(self, flow_id: str) -> JsonObject:
        flow = self.get_flow(flow_id, include_draft=True)
        if flow is None:
            raise ValidationError(f"Workflow not found: {flow_id}")
        return self.save_flow(flow, status="published")

    def create_submission(self, payload: JsonObject) -> JsonObject:
        return self.upsert_submission(payload, status="submitted", require_complete=True)

    def save_submission_progress(self, payload: JsonObject) -> JsonObject:
        return self.upsert_submission(payload, status="draft", require_complete=False)

    def get_or_create_participant_submission(self, payload: JsonObject) -> JsonObject:
        flow_id, flow, participant, participant_key = self._submission_context(payload)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT * FROM submissions
                WHERE flow_id = ? AND participant_key = ?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """,
                (flow_id, participant_key),
            ).fetchone()
            if row:
                existing_order = parse_json_list(row["video_order_json"])
                video_order = normalize_video_order(existing_order, flow)
                if video_order != existing_order:
                    db.execute(
                        "UPDATE submissions SET video_order_json = ? WHERE id = ?",
                        (json.dumps(video_order, ensure_ascii=False), row["id"]),
                    )
                    row = db.execute("SELECT * FROM submissions WHERE id = ?", (row["id"],)).fetchone()
                return self._row_to_submission(row)
        return self.upsert_submission(
            {
                "flow_id": flow_id,
                "participant": participant,
                "answers": {},
            },
            status="draft",
            require_complete=False,
        )

    def upsert_submission(self, payload: JsonObject, status: str, require_complete: bool) -> JsonObject:
        if status not in {"draft", "submitted"}:
            raise ValidationError("Submission status must be draft or submitted")
        flow_id, flow, participant, participant_key = self._submission_context(payload)
        incoming_answers = payload.get("answers")
        if not isinstance(incoming_answers, dict):
            raise ValidationError("answers must be an object")
        incoming_answers = remove_cancelled_answers(flow, incoming_answers)
        incoming_answers = normalize_answer_references(incoming_answers)
        changed_answer_keys = payload.get("changed_answer_keys")
        if changed_answer_keys is not None:
            if not isinstance(changed_answer_keys, list) or not all(
                isinstance(key, str) for key in changed_answer_keys
            ):
                raise ValidationError("changed_answer_keys must be an array of strings")
            changed_answer_keys = [
                key for key in dict.fromkeys(changed_answer_keys) if not answer_key_is_cancelled(flow, key)
            ]

        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                """
                SELECT *
                FROM submissions
                WHERE flow_id = ? AND participant_key = ?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """,
                (flow_id, participant_key),
            ).fetchone()
            if existing:
                submission_id = str(existing["id"])
                created_at = str(existing["created_at"])
                existing_answers = remove_cancelled_answers(
                    flow,
                    parse_json_object(existing["answers_json"], "answers_json"),
                )
                if changed_answer_keys is None:
                    answers = dict(incoming_answers)
                else:
                    answers = dict(existing_answers)
                    for key in changed_answer_keys:
                        if key in incoming_answers:
                            answers[key] = incoming_answers[key]
                        else:
                            answers.pop(key, None)
                answer_reviews = remove_cancelled_answers(
                    flow,
                    parse_json_object(existing["answer_reviews_json"], "answer_reviews_json"),
                )
                answer_reviews = resolve_changed_answer_reviews(flow, answer_reviews, answers, now)
                existing_unknown_keys = set(existing_answers) - expected_answer_keys(flow)
                self.validate_submission(
                    flow,
                    participant,
                    answers,
                    require_complete=require_complete,
                    answer_reviews=answer_reviews,
                    allowed_unknown_keys=existing_unknown_keys,
                )
                video_order = normalize_video_order(parse_json_list(existing["video_order_json"]), flow)
                db.execute(
                    """
                    UPDATE submissions
                    SET flow_version = ?,
                        participant_json = ?,
                        answers_json = ?,
                        video_order_json = ?,
                        answer_reviews_json = ?,
                        status = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        int(flow.get("version", 1)),
                        json.dumps(participant, ensure_ascii=False),
                        json.dumps(answers, ensure_ascii=False),
                        json.dumps(video_order, ensure_ascii=False),
                        json.dumps(answer_reviews, ensure_ascii=False),
                        status,
                        now,
                        submission_id,
                    ),
                )
            else:
                answers = remove_cancelled_answers(flow, incoming_answers)
                answer_reviews: JsonObject = {}
                self.validate_submission(
                    flow,
                    participant,
                    answers,
                    require_complete=require_complete,
                    answer_reviews=answer_reviews,
                )
                video_order = normalize_video_order([], flow)
                submission_id = str(uuid.uuid4())
                created_at = now
                db.execute(
                    """
                    INSERT INTO submissions (
                        id,
                        flow_id,
                        flow_version,
                        participant_key,
                        participant_json,
                        answers_json,
                        video_order_json,
                        answer_reviews_json,
                        status,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        submission_id,
                        flow_id,
                        int(flow.get("version", 1)),
                        participant_key,
                        json.dumps(participant, ensure_ascii=False),
                        json.dumps(answers, ensure_ascii=False),
                        json.dumps(video_order, ensure_ascii=False),
                        json.dumps(answer_reviews, ensure_ascii=False),
                        status,
                        created_at,
                        now,
                    ),
                )
        submission = self.get_submission(submission_id)
        if submission is None:
            raise RuntimeError("Saved submission could not be read")
        return submission

    def _submission_context(self, payload: JsonObject) -> tuple[str, JsonObject, JsonObject, str]:
        flow_id = str(payload.get("flow_id", "")).strip()
        flow = self.get_flow(flow_id, include_draft=False)
        if flow is None:
            raise ValidationError("Published workflow not found")
        participant = payload.get("participant")
        if not isinstance(participant, dict):
            raise ValidationError("participant must be an object")
        participant_key = participant_key_for_flow(flow, participant)
        return flow_id, flow, participant, participant_key

    def get_submission(self, submission_id: str) -> JsonObject | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
        return self._row_to_submission(row) if row else None

    def create_principal_session(self, values: JsonObject) -> JsonObject:
        session_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute(
                """
                DELETE FROM participant_sessions
                WHERE expires_at <= ?
                """,
                (utc_now(),),
            )
            db.execute(
                """
                INSERT INTO participant_sessions (
                    id,
                    token_hash,
                    principal_type,
                    flow_id,
                    participant_key,
                    canonical_participant_name,
                    submission_id,
                    allowlist_hash_at_issue,
                    created_at,
                    expires_at,
                    last_seen_at,
                    last_ip_hash,
                    user_agent_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    str(values["token_hash"]),
                    str(values["principal_type"]),
                    str(values.get("flow_id", "")),
                    str(values.get("participant_key", "")),
                    str(values.get("canonical_participant_name", "")),
                    str(values.get("submission_id", "")),
                    str(values.get("allowlist_hash_at_issue", "")),
                    str(values["created_at"]),
                    str(values["expires_at"]),
                    str(values["last_seen_at"]),
                    str(values.get("last_ip_hash", "")),
                    str(values.get("user_agent_hash", "")),
                ),
            )
            row = db.execute(
                "SELECT * FROM participant_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("New participant session could not be read")
        return dict(row)

    def get_principal_session(self, token_hash: str) -> JsonObject | None:
        now = utc_now()
        with self.connect() as db:
            row = db.execute(
                """
                SELECT *
                FROM participant_sessions
                WHERE token_hash = ?
                  AND revoked_at IS NULL
                  AND expires_at > ?
                """,
                (token_hash, now),
            ).fetchone()
        return dict(row) if row else None

    def revoke_principal_session(self, token_hash: str) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE participant_sessions
                SET revoked_at = ?
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (utc_now(), token_hash),
            )

    def get_daily_traffic_usage(
        self,
        flow_id: str,
        participant_key: str,
        local_date: str,
    ) -> JsonObject:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT *
                FROM daily_traffic_usage
                WHERE flow_id = ? AND participant_key = ? AND local_date = ?
                """,
                (flow_id, participant_key, local_date),
            ).fetchone()
        return dict(row) if row else {}

    def increment_daily_traffic_usage(
        self,
        flow_id: str,
        participant_key: str,
        local_date: str,
        *,
        deltas: dict[str, int],
        traffic_tier: str,
        refresh_tier: str,
        effective_factor: float,
    ) -> None:
        allowed_fields = {
            "egress_bytes",
            "video_bytes",
            "preview_bytes",
            "text_bytes",
            "api_static_bytes",
            "video_request_count",
            "range_request_count",
            "rejected_request_count",
            "document_load_count",
            "reported_reload_count",
        }
        normalized = {
            field: int(value)
            for field, value in deltas.items()
            if field in allowed_fields and int(value) != 0
        }
        if not normalized:
            return
        now = utc_now()
        assignments = [f"{field} = {field} + ?" for field in normalized]
        params: list[Any] = list(normalized.values())
        assignments.extend(
            [
                "current_traffic_tier = ?",
                "current_refresh_tier = ?",
                "effective_factor = ?",
                "last_seen_at = ?",
            ]
        )
        params.extend([traffic_tier, refresh_tier, effective_factor, now])
        params.extend([flow_id, participant_key, local_date])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT OR IGNORE INTO daily_traffic_usage (
                    flow_id,
                    participant_key,
                    local_date,
                    first_seen_at,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (flow_id, participant_key, local_date, now, now),
            )
            db.execute(
                f"""
                UPDATE daily_traffic_usage
                SET {", ".join(assignments)}
                WHERE flow_id = ? AND participant_key = ? AND local_date = ?
                """,
                tuple(params),
            )

    def record_traffic_page_event(
        self,
        values: JsonObject,
    ) -> tuple[bool, JsonObject]:
        flow_id = str(values["flow_id"])
        participant_key = str(values["participant_key"])
        local_date = str(values["local_date"])
        navigation_type = str(values.get("navigation_type", "navigate"))
        now = str(values["created_at"])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO traffic_page_events (
                    page_instance_id,
                    session_id,
                    flow_id,
                    participant_key,
                    event_type,
                    navigation_type,
                    created_at,
                    local_date
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(values["page_instance_id"]),
                    str(values["session_id"]),
                    flow_id,
                    participant_key,
                    str(values.get("event_type", "page_load")),
                    navigation_type,
                    now,
                    local_date,
                ),
            )
            inserted = cursor.rowcount == 1
            if inserted:
                db.execute(
                    """
                    INSERT OR IGNORE INTO daily_traffic_usage (
                        flow_id,
                        participant_key,
                        local_date,
                        first_seen_at,
                        last_seen_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (flow_id, participant_key, local_date, now, now),
                )
                reload_increment = 1 if navigation_type == "reload" else 0
                db.execute(
                    """
                    UPDATE daily_traffic_usage
                    SET document_load_count = document_load_count + 1,
                        reported_reload_count = reported_reload_count + ?,
                        last_seen_at = ?
                    WHERE flow_id = ? AND participant_key = ? AND local_date = ?
                    """,
                    (
                        reload_increment,
                        now,
                        flow_id,
                        participant_key,
                        local_date,
                    ),
                )
            row = db.execute(
                """
                SELECT *
                FROM daily_traffic_usage
                WHERE flow_id = ? AND participant_key = ? AND local_date = ?
                """,
                (flow_id, participant_key, local_date),
            ).fetchone()
        return inserted, dict(row) if row else {}

    def get_participant_manual_factor(
        self,
        flow_id: str,
        participant_key: str,
    ) -> float:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT manual_factor, manual_blocked_until
                FROM participant_access_policy
                WHERE flow_id = ? AND participant_key = ?
                """,
                (flow_id, participant_key),
            ).fetchone()
        if not row:
            return 1.0
        blocked_until = str(row["manual_blocked_until"] or "")
        if blocked_until and blocked_until > utc_now():
            return 0.0
        return min(1.0, max(0.0, float(row["manual_factor"])))

    def create_traffic_alert(self, values: JsonObject) -> None:
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO traffic_alerts (
                    id,
                    flow_id,
                    participant_key,
                    local_date,
                    alert_type,
                    tier,
                    observed_value,
                    message,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    str(values["flow_id"]),
                    str(values["participant_key"]),
                    str(values["local_date"]),
                    str(values["alert_type"]),
                    str(values["tier"]),
                    int(values["observed_value"]),
                    str(values["message"]),
                    now,
                ),
            )

    def list_daily_traffic_usage(self, local_date: str) -> list[JsonObject]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT usage.*,
                       (
                           SELECT participant_json
                           FROM submissions
                           WHERE submissions.flow_id = usage.flow_id
                             AND submissions.participant_key = usage.participant_key
                           ORDER BY submissions.updated_at DESC
                           LIMIT 1
                       ) AS participant_json
                FROM daily_traffic_usage AS usage
                WHERE usage.local_date = ?
                ORDER BY usage.egress_bytes DESC, usage.reported_reload_count DESC
                """,
                (local_date,),
            ).fetchall()
        result: list[JsonObject] = []
        for row in rows:
            item = dict(row)
            participant = parse_json_object(item.pop("participant_json", "{}"), "participant_json")
            item["participant_name"] = participant_display_name(participant)
            result.append(item)
        return result

    def list_traffic_alerts(
        self,
        local_date: str | None = None,
        unacknowledged_only: bool = False,
    ) -> list[JsonObject]:
        conditions: list[str] = []
        params: list[Any] = []
        if local_date:
            conditions.append("alerts.local_date = ?")
            params.append(local_date)
        if unacknowledged_only:
            conditions.append("alerts.acknowledged_at IS NULL")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT alerts.*,
                       (
                           SELECT participant_json
                           FROM submissions
                           WHERE submissions.flow_id = alerts.flow_id
                             AND submissions.participant_key = alerts.participant_key
                           ORDER BY submissions.updated_at DESC
                           LIMIT 1
                       ) AS participant_json
                FROM traffic_alerts AS alerts
                {where}
                ORDER BY alerts.acknowledged_at IS NOT NULL,
                         alerts.created_at DESC
                """,
                tuple(params),
            ).fetchall()
        result: list[JsonObject] = []
        for row in rows:
            item = dict(row)
            participant = parse_json_object(item.pop("participant_json", "{}"), "participant_json")
            item["participant_name"] = participant_display_name(participant)
            result.append(item)
        return result

    def acknowledge_traffic_alert(
        self,
        alert_id: str,
        acknowledged_by: str = "admin",
    ) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE traffic_alerts
                SET acknowledged_at = ?, acknowledged_by = ?
                WHERE id = ? AND acknowledged_at IS NULL
                """,
                (utc_now(), acknowledged_by, alert_id),
            )
        return cursor.rowcount > 0

    def get_participant_submission(self, flow_id: str, participant: JsonObject) -> JsonObject | None:
        flow = self.get_flow(flow_id, include_draft=False)
        if flow is None:
            raise ValidationError("Published workflow not found")
        participant_key = participant_key_for_flow(flow, participant)
        with self.connect() as db:
            row = db.execute(
                """
                SELECT *
                FROM submissions
                WHERE flow_id = ? AND participant_key = ?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """,
                (flow_id, participant_key),
            ).fetchone()
        return self._row_to_submission(row) if row else None

    def list_submissions(self, flow_id: str | None = None, include_hidden: bool = False) -> list[JsonObject]:
        query = "SELECT * FROM submissions"
        conditions: list[str] = []
        params: list[Any] = []
        if flow_id:
            conditions.append("flow_id = ?")
            params.append(flow_id)
        if not include_hidden:
            conditions.append("is_hidden = 0")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY is_pinned DESC, updated_at DESC, created_at DESC"
        with self.connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()
        return [self._row_to_submission(row) for row in rows]

    def update_submission_admin_flags(
        self,
        submission_id: str,
        *,
        is_pinned: bool | None = None,
        is_hidden: bool | None = None,
    ) -> JsonObject | None:
        updates: list[str] = []
        params: list[Any] = []
        if is_pinned is not None:
            updates.append("is_pinned = ?")
            params.append(1 if is_pinned else 0)
        if is_hidden is not None:
            updates.append("is_hidden = ?")
            params.append(1 if is_hidden else 0)
        if not updates:
            raise ValidationError("At least one administration flag is required")

        with self.connect() as db:
            existing = db.execute("SELECT id FROM submissions WHERE id = ?", (submission_id,)).fetchone()
            if not existing:
                return None
            db.execute(
                f"UPDATE submissions SET {', '.join(updates)} WHERE id = ?",
                tuple(params + [submission_id]),
            )
        return self.get_submission(submission_id)

    def mark_submission_answer_for_revision(
        self,
        submission_id: str,
        answer_key_value: str,
        comment: str,
    ) -> JsonObject | None:
        answer_key_value = str(answer_key_value or "").strip()
        comment = str(comment or "").strip()
        if not answer_key_value:
            raise ValidationError("answer_key is required")
        if not comment:
            raise ValidationError("comment is required")
        if len(comment) > 2000:
            raise ValidationError("comment cannot exceed 2000 characters")

        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
            if not row:
                return None
            flow_row = db.execute("SELECT definition_json FROM flows WHERE id = ?", (row["flow_id"],)).fetchone()
            if not flow_row:
                raise ValidationError("Workflow for the submission was not found")
            flow = json.loads(str(flow_row["definition_json"]))
            if answer_key_value not in expected_answer_keys(flow):
                raise ValidationError("Unrecognized answer_key")
            answers = parse_json_object(row["answers_json"], "answers_json")
            answer = answers.get(answer_key_value)
            if not isinstance(answer, dict):
                raise ValidationError("An unanswered item cannot be marked for revision")
            reviews = parse_json_object(row["answer_reviews_json"], "answer_reviews_json")
            reviews[answer_key_value] = {
                "status": "needs_revision",
                "comment": comment,
                "marked_at": now,
                "marked_answer": answer,
            }
            db.execute(
                """
                UPDATE submissions
                SET answer_reviews_json = ?, status = 'draft', updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(reviews, ensure_ascii=False), now, submission_id),
            )
        return self.get_submission(submission_id)

    def export_submissions_csv(self, flow_id: str | None = None) -> str:
        submissions = self.list_submissions(flow_id, include_hidden=True)
        output = io.StringIO()
        fieldnames = [
            "submission_id",
            "created_at",
            "updated_at",
            "status",
            "is_hidden",
            "is_pinned",
            "video_order_json",
            "flow_id",
            "flow_version",
            "participant_json",
            "video_id",
            "dimension_id",
            "question_id",
            "score",
            "confidence",
            "explanation",
            "review_status",
            "admin_comment",
            "review_marked_at",
            "review_resolved_at",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for submission in submissions:
            flow = self.get_flow(str(submission["flow_id"]), include_draft=True)
            answers = remove_cancelled_answers(flow or {}, submission["answers"])
            if not answers:
                writer.writerow(self._csv_base_row(submission))
                continue
            for answer_key, answer in answers.items():
                video_id, dimension_id, question_id = split_answer_key(answer_key)
                row = self._csv_base_row(submission)
                review = submission.get("answer_reviews", {}).get(answer_key, {})
                row.update(
                    {
                        "video_id": video_id,
                        "dimension_id": dimension_id,
                        "question_id": question_id,
                        "score": answer.get("score", ""),
                        "confidence": answer.get("confidence", ""),
                        "explanation": answer.get("explanation", ""),
                        "review_status": review.get("status", ""),
                        "admin_comment": review.get("comment", ""),
                        "review_marked_at": review.get("marked_at", ""),
                        "review_resolved_at": review.get("resolved_at", ""),
                    }
                )
                writer.writerow(row)
        return output.getvalue()

    def validate_flow(self, flow: JsonObject) -> None:
        if not isinstance(flow, dict):
            raise ValidationError("Workflow must be an object")
        required = ["id", "title", "participantFields", "instructions", "videos", "dimensions"]
        for key in required:
            if key not in flow:
                raise ValidationError(f"Workflow is missing required field: {key}")
        if not isinstance(flow["participantFields"], list):
            raise ValidationError("participantFields must be a list")
        if not isinstance(flow["videos"], list) or not flow["videos"]:
            raise ValidationError("videos must be a non-empty list")
        if not isinstance(flow["dimensions"], list) or not flow["dimensions"]:
            raise ValidationError("dimensions must be a non-empty list")
        for video in flow["videos"]:
            require_object_fields(video, ["id", "title"], "video")
        for dimension in flow["dimensions"]:
            require_object_fields(dimension, ["id", "title", "questions"], "dimension")
            if not isinstance(dimension["questions"], list) or not dimension["questions"]:
                raise ValidationError(f"Dimension {dimension['id']} must contain questions")
            for question in dimension["questions"]:
                require_object_fields(question, ["id", "prompt"], "question")

    def validate_submission(
        self,
        flow: JsonObject,
        participant: JsonObject,
        answers: JsonObject,
        require_complete: bool = True,
        answer_reviews: JsonObject | None = None,
        allowed_unknown_keys: set[str] | None = None,
    ) -> None:
        for field in flow.get("participantFields", []):
            field_id = field.get("id")
            if field.get("required") and not str(participant.get(field_id, "")).strip():
                raise ValidationError(f"Missing participant field: {field_id}")

        expected_keys: set[str] = set()
        missing: list[str] = []
        for video in flow.get("videos", []):
            if video_is_cancelled(video):
                continue
            for dimension in flow.get("dimensions", []):
                for question in dimension.get("questions", []):
                    key = answer_key(video["id"], dimension["id"], question["id"])
                    expected_keys.add(key)
                    answer = answers.get(key)
                    if not require_complete and answer is None:
                        continue
                    if not isinstance(answer, dict):
                        missing.append(key)
                        continue
                    if not require_complete:
                        continue
                    if answer.get("score") in ("", None):
                        missing.append(f"{key}.score")
                    if answer.get("confidence") in ("", None):
                        missing.append(f"{key}.confidence")
                    if question.get("explanationRequired", flow.get("responseConfig", {}).get("explanationRequired", False)):
                        if not str(answer.get("explanation", "")).strip():
                            missing.append(f"{key}.explanation")
                    review = (answer_reviews or {}).get(key, {})
                    if isinstance(review, dict) and review.get("status") == "needs_revision":
                        missing.append(f"{key}.needs_revision")
        if missing:
            raise ValidationError("Missing required answers: " + ", ".join(missing[:10]))
        unknown_keys = sorted(set(answers) - expected_keys - (allowed_unknown_keys or set()))
        if unknown_keys:
            raise ValidationError("Unrecognized answer keys: " + ", ".join(unknown_keys[:10]))

    def _row_to_flow(self, row: sqlite3.Row) -> JsonObject:
        flow = json.loads(str(row["definition_json"]))
        flow["id"] = row["id"]
        flow["title"] = row["title"]
        flow["status"] = row["status"]
        flow["version"] = row["version"]
        flow["created_at"] = row["created_at"]
        flow["updated_at"] = row["updated_at"]
        flow["published_at"] = row["published_at"]
        return flow

    def _row_to_submission(self, row: sqlite3.Row) -> JsonObject:
        return {
            "id": row["id"],
            "flow_id": row["flow_id"],
            "flow_version": row["flow_version"],
            "participant_key": row["participant_key"],
            "participant": json.loads(str(row["participant_json"])),
            "answers": json.loads(str(row["answers_json"])),
            "status": row["status"],
            "is_hidden": bool(row["is_hidden"]),
            "is_pinned": bool(row["is_pinned"]),
            "video_order": parse_json_list(row["video_order_json"]),
            "answer_reviews": parse_json_object(row["answer_reviews_json"], "answer_reviews_json"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _csv_base_row(self, submission: JsonObject) -> JsonObject:
        return {
            "submission_id": submission["id"],
            "created_at": submission["created_at"],
            "updated_at": submission["updated_at"],
            "status": submission["status"],
            "is_hidden": 1 if submission.get("is_hidden") else 0,
            "is_pinned": 1 if submission.get("is_pinned") else 0,
            "video_order_json": json.dumps(submission.get("video_order", []), ensure_ascii=False),
            "flow_id": submission["flow_id"],
            "flow_version": submission["flow_version"],
            "participant_json": json.dumps(submission["participant"], ensure_ascii=False),
            "video_id": "",
            "dimension_id": "",
            "question_id": "",
            "score": "",
            "confidence": "",
            "explanation": "",
            "review_status": "",
            "admin_comment": "",
            "review_marked_at": "",
            "review_resolved_at": "",
        }


def parse_json_object(value: Any, label: str) -> JsonObject:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValidationError(f"{label} must contain a JSON object")
    return parsed


def parse_json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def expected_answer_keys(flow: JsonObject) -> set[str]:
    return {
        answer_key(video["id"], dimension["id"], question["id"])
        for video in flow.get("videos", [])
        if not video_is_cancelled(video)
        for dimension in flow.get("dimensions", [])
        for question in dimension.get("questions", [])
    }


def video_is_cancelled(video: Any) -> bool:
    if not isinstance(video, dict):
        return False
    return bool(
        video.get("cancelled")
        or video.get("isCancelled")
        or str(video.get("status", "")).strip().lower() == "cancelled"
    )


def cancelled_video_ids(flow: JsonObject) -> set[str]:
    return {str(video.get("id")) for video in flow.get("videos", []) if video_is_cancelled(video)}


def answer_key_is_cancelled(flow: JsonObject, key: str) -> bool:
    video_id, _, _ = split_answer_key(str(key))
    return video_id in cancelled_video_ids(flow)


def remove_cancelled_answers(flow: JsonObject, answers: JsonObject) -> JsonObject:
    if not isinstance(answers, dict):
        return {}
    cancelled_ids = cancelled_video_ids(flow)
    if not cancelled_ids:
        return dict(answers)
    return {key: value for key, value in answers.items() if split_answer_key(str(key))[0] not in cancelled_ids}


def normalize_video_order(value: Any, flow: JsonObject) -> list[str]:
    video_ids = [str(video["id"]) for video in flow.get("videos", [])]
    available = set(video_ids)
    existing = []
    if isinstance(value, list):
        for item in value:
            video_id = str(item)
            if video_id in available and video_id not in existing:
                existing.append(video_id)
    missing = [video_id for video_id in video_ids if video_id not in existing]
    random.SystemRandom().shuffle(missing)
    return existing + missing


def resolve_changed_answer_reviews(
    flow: JsonObject,
    reviews: JsonObject,
    answers: JsonObject,
    resolved_at: str,
) -> JsonObject:
    normalized = dict(reviews)
    for key, review in list(normalized.items()):
        if not isinstance(review, dict) or review.get("status") not in {"needs_revision", "resolved"}:
            continue
        answer = answers.get(key)
        if answers_equal(answer, review.get("marked_answer")) or not answer_is_complete(flow, key, answer):
            pending_review = dict(review)
            pending_review["status"] = "needs_revision"
            pending_review.pop("resolved_at", None)
            pending_review.pop("revised_answer", None)
            normalized[key] = pending_review
            continue
        normalized[key] = {
            **review,
            "status": "resolved",
            "resolved_at": resolved_at,
            "revised_answer": answer,
        }
    return normalized


def answers_equal(left: Any, right: Any) -> bool:
    def normalize(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "references" not in value and "explanation_body" not in value:
            return normalized
        references = value.get("references", [])
        if not isinstance(references, list):
            return normalized
        explanation_body = value.get("explanation_body", value.get("explanation", ""))
        if not isinstance(explanation_body, str):
            return normalized
        valid_references = [
            reference
            for reference in references
            if isinstance(reference, dict) and str(reference.get("id", "")).strip()
        ]
        normalized["reference_placements"] = normalize_reference_placements(
            value.get("reference_placements"),
            valid_references,
            explanation_body,
        )
        normalized.pop("explanation", None)
        return normalized

    return json.dumps(normalize(left), ensure_ascii=False, sort_keys=True) == json.dumps(
        normalize(right),
        ensure_ascii=False,
        sort_keys=True,
    )


def answer_is_complete(flow: JsonObject, key: str, answer: Any) -> bool:
    if answer_key_is_cancelled(flow, key):
        return False
    if not isinstance(answer, dict):
        return False
    if answer.get("score") in ("", None) or answer.get("confidence") in ("", None):
        return False
    for video in flow.get("videos", []):
        for dimension in flow.get("dimensions", []):
            for question in dimension.get("questions", []):
                if answer_key(video["id"], dimension["id"], question["id"]) != key:
                    continue
                explanation_required = question.get(
                    "explanationRequired",
                    flow.get("responseConfig", {}).get("explanationRequired", False),
                )
                return not explanation_required or bool(str(answer.get("explanation", "")).strip())
    return False


def require_object_fields(value: Any, fields: list[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be an object")
    for field in fields:
        if field not in value or value[field] in ("", None):
            raise ValidationError(f"{label} is missing required field: {field}")


def answer_key(video_id: str, dimension_id: str, question_id: str) -> str:
    return f"{video_id}:{dimension_id}:{question_id}"


def split_answer_key(key: str) -> tuple[str, str, str]:
    parts = key.split(":", 2)
    if len(parts) != 3:
        return key, "", ""
    return parts[0], parts[1], parts[2]


def normalize_answer_references(answers: JsonObject) -> JsonObject:
    normalized_answers: JsonObject = {}
    for key, answer in answers.items():
        if not isinstance(answer, dict):
            normalized_answers[key] = answer
            continue
        if "references" not in answer and "explanation_body" not in answer:
            normalized_answers[key] = answer
            continue

        references = answer.get("references", [])
        if not isinstance(references, list):
            raise ValidationError(f"{key}.references must be an array")
        explanation_body = answer.get("explanation_body", answer.get("explanation", ""))
        if not isinstance(explanation_body, str):
            raise ValidationError(f"{key}.explanation_body must be a string")

        video_id, _, _ = split_answer_key(str(key))
        normalized_references: list[JsonObject] = []
        reference_ids: set[str] = set()
        video_time_references: set[tuple[str, int]] = set()
        total_chars = 0
        for index, reference in enumerate(references, start=1):
            label = f"{key}.references[{index - 1}]"
            if not isinstance(reference, dict):
                raise ValidationError(f"{label} must be an object")
            reference_id = str(reference.get("id", "")).strip()
            reference_video_id = str(reference.get("video_id", "")).strip()
            reference_type = str(reference.get("type", "text")).strip().lower() or "text"

            if reference_type not in {"text", "video_time"}:
                raise ValidationError(f"{label}.type must be text or video_time")
            if not reference_id or len(reference_id) > 128 or reference_id in reference_ids:
                raise ValidationError(f"{label}.id must be unique and at most 128 characters")
            if reference_video_id != video_id:
                raise ValidationError(f"{label}.video_id must match the answer video")

            if reference_type == "video_time":
                time_seconds = reference.get("time_seconds")
                if (
                    not isinstance(time_seconds, int)
                    or isinstance(time_seconds, bool)
                    or time_seconds < 0
                ):
                    raise ValidationError(
                        f"{label}.time_seconds must be a non-negative integer"
                    )
                video_time_key = (reference_video_id, time_seconds)
                if video_time_key in video_time_references:
                    raise ValidationError(
                        f"{label} duplicates an existing video time reference"
                    )
                reference_ids.add(reference_id)
                video_time_references.add(video_time_key)
                normalized_references.append(
                    {
                        "id": reference_id,
                        "type": "video_time",
                        "video_id": reference_video_id,
                        "time_seconds": time_seconds,
                    }
                )
                continue

            language = str(reference.get("language", "")).strip().lower()
            source_key = str(reference.get("source_key", "")).strip()
            text = reference.get("text")
            prefix = reference.get("prefix", "")
            suffix = reference.get("suffix", "")
            start = reference.get("start")
            end = reference.get("end")
            source_length = reference.get("source_length")

            if language not in {"original", "translation"}:
                raise ValidationError(
                    f"{label}.language must be original or translation"
                )
            if not source_key or len(source_key) > 2048:
                raise ValidationError(f"{label}.source_key is invalid")
            if not isinstance(start, int) or isinstance(start, bool) or start < 0:
                raise ValidationError(f"{label}.start must be a non-negative integer")
            if not isinstance(end, int) or isinstance(end, bool) or end <= start:
                raise ValidationError(f"{label}.end must be greater than start")
            if source_length is not None and (
                not isinstance(source_length, int)
                or isinstance(source_length, bool)
                or source_length < end
            ):
                raise ValidationError(f"{label}.source_length must be an integer no smaller than end")
            if not isinstance(text, str) or not text.strip():
                raise ValidationError(f"{label}.text must be a non-empty string")
            if len(text) > TEXT_REFERENCE_MAX_CHARS:
                raise ValidationError(
                    f"Each text reference may contain at most {TEXT_REFERENCE_MAX_CHARS} characters"
                )
            if not isinstance(prefix, str) or len(prefix) > 100:
                raise ValidationError(f"{label}.prefix must be a string of at most 100 characters")
            if not isinstance(suffix, str) or len(suffix) > 100:
                raise ValidationError(f"{label}.suffix must be a string of at most 100 characters")

            total_chars += len(text)
            if total_chars > TEXT_REFERENCE_TOTAL_MAX_CHARS:
                raise ValidationError(
                    f"Text references for one answer may total at most "
                    f"{TEXT_REFERENCE_TOTAL_MAX_CHARS} characters"
                )
            reference_ids.add(reference_id)
            normalized_references.append(
                {
                    "id": reference_id,
                    "video_id": reference_video_id,
                    "language": language,
                    "source_key": source_key,
                    "start": start,
                    "end": end,
                    "source_length": (
                        source_length
                        if source_length is not None
                        else max(end, end + len(suffix))
                    ),
                    "text": text,
                    "prefix": prefix,
                    "suffix": suffix,
                }
            )

        normalized_answer = {
            **answer,
            "explanation_body": explanation_body,
            "references": normalized_references,
        }
        normalized_placements = normalize_reference_placements(
            answer.get("reference_placements"),
            normalized_references,
            explanation_body,
        )
        normalized_answer["reference_placements"] = normalized_placements
        normalized_answer["explanation"] = compose_answer_explanation(
            normalized_references,
            explanation_body,
            normalized_placements,
        )
        normalized_answers[key] = normalized_answer
    return normalized_answers


def normalize_reference_placements(
    placements: Any,
    references: list[JsonObject],
    explanation_body: str,
) -> list[JsonObject]:
    body_length = len(explanation_body)
    reference_ids = {str(reference["id"]) for reference in references}
    normalized: list[JsonObject] = []
    seen_ids: set[str] = set()
    if isinstance(placements, list):
        for placement in placements:
            if not isinstance(placement, dict):
                continue
            reference_id = str(placement.get("reference_id", ""))
            offset = placement.get("offset")
            if (
                reference_id not in reference_ids
                or reference_id in seen_ids
                or not isinstance(offset, int)
                or isinstance(offset, bool)
                or offset < 0
                or offset > body_length
            ):
                continue
            normalized.append({"reference_id": reference_id, "offset": offset})
            seen_ids.add(reference_id)
    for reference in references:
        reference_id = str(reference["id"])
        if reference_id not in seen_ids:
            normalized.append({"reference_id": reference_id, "offset": body_length})
    return [
        placement
        for _, placement in sorted(
            enumerate(normalized),
            key=lambda item: (int(item[1]["offset"]), item[0]),
        )
    ]


def compose_answer_explanation(
    references: list[JsonObject],
    explanation_body: str,
    placements: list[JsonObject] | None = None,
) -> str:
    if not references:
        return explanation_body.strip()
    normalized_placements = normalize_reference_placements(
        placements,
        references,
        explanation_body,
    )
    references_by_id = {str(reference["id"]): reference for reference in references}
    parts: list[str] = []
    body_offset = 0
    for placement in normalized_placements:
        offset = int(placement["offset"])
        if offset > body_offset:
            parts.append(explanation_body[body_offset:offset])
        reference = references_by_id.get(str(placement["reference_id"]))
        if reference:
            parts.append(format_answer_reference(reference))
        body_offset = offset
    parts.append(explanation_body[body_offset:])
    return "".join(parts).strip()


def format_answer_reference(reference: JsonObject) -> str:
    if str(reference.get("type", "")).strip().lower() == "video_time":
        return f"'''Video[{format_video_reference_time(reference.get('time_seconds', 0))}]'''"
    start = max(0, int(reference.get("start", 0)))
    end = max(start, int(reference.get("end", start)))
    source_length_value = reference.get("source_length")
    if (
        isinstance(source_length_value, int)
        and not isinstance(source_length_value, bool)
        and source_length_value > 0
    ):
        source_length = source_length_value
    else:
        source_length = max(end, end + len(str(reference.get("suffix", ""))))
    position = 0
    if source_length > 0:
        position = min(100, (min(start, source_length) * 100 + source_length // 2) // source_length)
    return f"'''[{position}%]: {reference.get('text', '')}'''"


def format_video_reference_time(value: Any) -> str:
    total_seconds = max(0, int(value))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def participant_key_for_flow(flow: JsonObject, participant: JsonObject) -> str:
    fields = flow.get("participantFields", [])
    preferred_ids = (
        "participant_code",
        "participant_name",
        "subject_code",
        "subject_name",
        "name",
        "username",
    )
    for preferred_id in preferred_ids:
        key = participant_key_from_field(preferred_id, participant)
        if key:
            return key
    for field in fields:
        field_id = str(field.get("id", "")).strip()
        if field.get("required"):
            key = participant_key_from_field(field_id, participant)
            if key:
                return key
    for field in fields:
        field_id = str(field.get("id", "")).strip()
        key = participant_key_from_field(field_id, participant)
        if key:
            return key
    if participant:
        return "participant:" + json.dumps(participant, ensure_ascii=False, sort_keys=True)
    raise ValidationError("Participant identity field is missing")


def participant_key_from_field(field_id: str, participant: JsonObject) -> str | None:
    if not field_id:
        return None
    value = str(participant.get(field_id, "")).strip()
    if not value:
        return None
    return f"{field_id}:{value.casefold()}"


def participant_display_name(participant: JsonObject) -> str:
    for field_id in (
        "participant_name",
        "participant_code",
        "name",
        "username",
        "subject_name",
        "subject_code",
    ):
        value = str(participant.get(field_id, "")).strip()
        if value:
            return value
    for value in participant.values():
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return "Unknown identifier"
