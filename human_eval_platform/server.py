from __future__ import annotations

import argparse
import hashlib
import json
import math
import mimetypes
import os
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .config import (
    AppConfig,
    ParticipantGovernanceConfig,
    TrafficProtectionConfig,
    load_config,
)
from .governance import (
    ADMIN_COOKIE_NAME,
    PARTICIPANT_COOKIE_NAME,
    ParticipantGovernance,
    RequestPrincipal,
    canonicalize_participant_name,
    policy_date,
    participant_name_from_payload,
)
from .store import EvaluationStore, ValidationError


ADMIN_PASSWORD = os.environ.get("HEP_ADMIN_PASSWORD", "").strip()
VIDEO_STREAM_CHUNK_BYTES = 256 * 1024
MIB = 1024 * 1024


class TokenBucketRateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}
        self._last_cleanup = 0.0

    def allow(
        self,
        scope: str,
        client_key: str,
        requests_per_minute: int,
        now: float | None = None,
    ) -> tuple[bool, float]:
        current = time.monotonic() if now is None else now
        rate_per_second = requests_per_minute / 60.0
        burst = max(10, min(requests_per_minute, requests_per_minute // 5))
        bucket_key = (scope, client_key)
        with self._lock:
            tokens, updated_at = self._buckets.get(bucket_key, (float(burst), current))
            tokens = min(float(burst), tokens + max(0.0, current - updated_at) * rate_per_second)
            if tokens >= 1.0:
                self._buckets[bucket_key] = (tokens - 1.0, current)
                allowed = True
                retry_after = 0.0
            else:
                self._buckets[bucket_key] = (tokens, current)
                allowed = False
                retry_after = (1.0 - tokens) / rate_per_second
            if current - self._last_cleanup >= 60.0 and len(self._buckets) > 1024:
                cutoff = current - 600.0
                self._buckets = {
                    key: state for key, state in self._buckets.items() if state[1] >= cutoff
                }
                self._last_cleanup = current
        return allowed, retry_after


class SharedBandwidthLimiter:
    def __init__(self, total_bytes_per_second: float, per_ip_bytes_per_second: float) -> None:
        self._total_rate = total_bytes_per_second
        self._per_ip_rate = per_ip_bytes_per_second
        current = time.monotonic()
        self._total_state = (total_bytes_per_second, current)
        self._per_ip_states: dict[str, tuple[float, float]] = {}
        self._per_participant_states: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._last_cleanup = current

    @staticmethod
    def _reserve(
        state: tuple[float, float],
        amount: int,
        rate: float,
        current: float,
    ) -> tuple[tuple[float, float], float]:
        tokens, updated_at = state
        tokens = min(rate, tokens + max(0.0, current - updated_at) * rate)
        tokens -= amount
        return (tokens, current), max(0.0, -tokens / rate)

    def wait(
        self,
        client_key: str,
        amount: int,
        participant_key: str | None = None,
        participant_rate: float | None = None,
    ) -> None:
        current = time.monotonic()
        with self._lock:
            self._total_state, total_delay = self._reserve(
                self._total_state,
                amount,
                self._total_rate,
                current,
            )
            per_ip_state = self._per_ip_states.get(client_key, (self._per_ip_rate, current))
            per_ip_state, per_ip_delay = self._reserve(
                per_ip_state,
                amount,
                self._per_ip_rate,
                current,
            )
            self._per_ip_states[client_key] = per_ip_state
            participant_delay = 0.0
            if participant_key and participant_rate and participant_rate > 0:
                participant_state = self._per_participant_states.get(
                    participant_key,
                    (participant_rate, current),
                )
                participant_state, participant_delay = self._reserve(
                    participant_state,
                    amount,
                    participant_rate,
                    current,
                )
                self._per_participant_states[participant_key] = participant_state
            if current - self._last_cleanup >= 60.0 and (
                len(self._per_ip_states) > 1024
                or len(self._per_participant_states) > 1024
            ):
                cutoff = current - 600.0
                self._per_ip_states = {
                    key: state for key, state in self._per_ip_states.items() if state[1] >= cutoff
                }
                self._per_participant_states = {
                    key: state
                    for key, state in self._per_participant_states.items()
                    if state[1] >= cutoff
                }
                self._last_cleanup = current
        delay = max(total_delay, per_ip_delay, participant_delay)
        if delay > 0:
            time.sleep(delay)


class TrafficGuard:
    def __init__(self, settings: TrafficProtectionConfig) -> None:
        self.settings = settings
        self._request_limiter = TokenBucketRateLimiter()
        self._video_lock = threading.Lock()
        self._active_videos_total = 0
        self._active_videos_by_ip: dict[str, int] = {}
        self._bandwidth_limiter = SharedBandwidthLimiter(
            settings.video_bandwidth_mib_per_second_total * MIB,
            settings.video_bandwidth_mib_per_second_per_ip * MIB,
        )

    def allow_request(self, path: str, client_key: str) -> tuple[bool, float]:
        if not self.settings.enabled:
            return True, 0.0
        if path.startswith("/videos/"):
            scope = "video"
            limit = self.settings.video_requests_per_minute_per_ip
        elif path.startswith("/api/"):
            scope = "api"
            limit = self.settings.api_requests_per_minute_per_ip
        else:
            scope = "other"
            limit = self.settings.other_requests_per_minute_per_ip
        return self._request_limiter.allow(scope, client_key, limit)

    def acquire_video(self, client_key: str) -> tuple[bool, str | None]:
        if not self.settings.enabled:
            return True, None
        with self._video_lock:
            client_count = self._active_videos_by_ip.get(client_key, 0)
            if client_count >= self.settings.max_video_connections_per_ip:
                return False, "per_ip"
            if self._active_videos_total >= self.settings.max_video_connections_total:
                return False, "total"
            self._active_videos_total += 1
            self._active_videos_by_ip[client_key] = client_count + 1
        return True, None

    def release_video(self, client_key: str) -> None:
        if not self.settings.enabled:
            return
        with self._video_lock:
            client_count = self._active_videos_by_ip.get(client_key, 0)
            if client_count <= 1:
                self._active_videos_by_ip.pop(client_key, None)
            else:
                self._active_videos_by_ip[client_key] = client_count - 1
            self._active_videos_total = max(0, self._active_videos_total - 1)

    def wait_for_video_bandwidth(
        self,
        client_key: str,
        amount: int,
        participant_key: str | None = None,
        participant_rate: float | None = None,
    ) -> None:
        if self.settings.enabled:
            self._bandwidth_limiter.wait(
                client_key,
                amount,
                participant_key=participant_key,
                participant_rate=participant_rate,
            )


class LimitedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, server_address, request_handler_class, max_connections: int) -> None:
        self._connection_slots = (
            threading.BoundedSemaphore(max_connections) if max_connections > 0 else None
        )
        super().__init__(server_address, request_handler_class)

    def process_request(self, request, client_address) -> None:
        if self._connection_slots is None:
            super().process_request(request, client_address)
            return
        if not self._connection_slots.acquire(blocking=False):
            try:
                payload = "Service busy. Please try again shortly.\n".encode("utf-8")
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: text/plain; charset=utf-8\r\n"
                    + f"Content-Length: {len(payload)}\r\n".encode("ascii")
                    +
                    b"Retry-After: 2\r\n"
                    b"Connection: close\r\n\r\n"
                    + payload
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            if self._connection_slots is not None:
                self._connection_slots.release()


def create_handler(
    config: AppConfig,
    store: EvaluationStore,
    governance: ParticipantGovernance | None = None,
):
    protection = getattr(config, "traffic_protection", TrafficProtectionConfig())
    traffic_guard = TrafficGuard(protection)
    if governance is None and config is None:
        class TestConfig:
            participant_governance = ParticipantGovernanceConfig(enabled=False)
            participant_allowlist_path = (
                Path(__file__).resolve().parent.parent
                / "docs"
                / "participant-allowlist.md"
            )

        governance = ParticipantGovernance(TestConfig(), store)
    governance = governance or ParticipantGovernance(config, store)
    video_cache_max_age = getattr(config, "video_cache_max_age_seconds", 31_536_000)

    class HumanEvalHandler(BaseHTTPRequestHandler):
        server_version = "HumanEvalPlatform/1.0"
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            if protection.enabled:
                self.connection.settimeout(protection.socket_timeout_seconds)

        def do_GET(self) -> None:
            self.handle_get()

        def do_HEAD(self) -> None:
            self.handle_get()

        def handle_get(self) -> None:
            self._request_principal_resolved = False
            self._request_principal = None
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if not self.check_request_rate(path):
                return
            try:
                if path == "/api/config":
                    self.send_json(
                        {
                            "videoBasePath": "/videos/",
                            "maxRequestBytes": 2_000_000,
                        }
                    )
                elif path == "/api/admin/check":
                    if not self.require_admin():
                        return
                    token, _ = self.ensure_admin_session()
                    headers = (
                        {
                            "Set-Cookie": self.session_cookie_header(
                                ADMIN_COOKIE_NAME,
                                token,
                            )
                        }
                        if token
                        else None
                    )
                    self.send_json({"ok": True}, extra_headers=headers)
                elif path == "/api/participant-session/current":
                    principal = self.require_participant_session(
                        require_allowlisted=True
                    )
                    if principal is None:
                        return
                    submission = store.get_submission(principal.submission_id)
                    if submission is None:
                        self.send_error_json(
                            HTTPStatus.NOT_FOUND,
                            "The evaluation record no longer exists. Please sign in again.",
                        )
                        return
                    self.send_json(
                        {
                            "submission": submission,
                            "participant_key": submission.get("participant_key"),
                            "usage": governance.policy_for(principal),
                        }
                    )
                elif path == "/api/usage/me":
                    principal = self.require_participant_session(
                        require_allowlisted=False
                    )
                    if principal is None:
                        return
                    usage = governance.policy_for(principal)
                    if not governance.participant_session_is_allowed(principal):
                        usage = {
                            **usage,
                            "blocked": True,
                            "would_block": True,
                            "messages": [
                                "This identifier is no longer on the allowlist. Current input will still be saved; contact an administrator."
                            ],
                        }
                    self.send_json({"usage": usage})
                elif path == "/api/admin/traffic/daily":
                    if not self.require_admin():
                        return
                    selected_date = first(query, "date") or policy_date()
                    self.send_json(
                        {
                            "usage": governance.admin_daily_usage(selected_date),
                            "alerts": store.list_traffic_alerts(
                                selected_date,
                                unacknowledged_only=False,
                            ),
                            "allowlist": governance.allowlist.status(),
                        }
                    )
                elif path == "/api/admin/traffic/alerts":
                    if not self.require_admin():
                        return
                    self.send_json(
                        {
                            "alerts": store.list_traffic_alerts(
                                first(query, "date"),
                                unacknowledged_only=first(query, "unacknowledged") == "1",
                            )
                        }
                    )
                elif path == "/api/admin/participant-allowlist/status":
                    if not self.require_admin():
                        return
                    self.send_json({"allowlist": governance.allowlist.status()})
                elif path == "/api/video-text":
                    if self.require_evaluation_media_access() is None and governance.settings.enabled:
                        return
                    self.serve_video_text(
                        video_path=first(query, "video_path"),
                        text_path=first(query, "text_path"),
                        language=first(query, "language"),
                    )
                elif path == "/api/video-preview":
                    if self.require_evaluation_media_access() is None and governance.settings.enabled:
                        return
                    self.serve_video_preview(first(query, "video_path"))
                elif path == "/api/flows":
                    include_drafts = query.get("include_drafts", ["0"])[0] == "1"
                    if include_drafts and not self.require_admin():
                        return
                    self.send_json({"flows": store.list_flows(include_drafts=include_drafts)})
                elif path.startswith("/api/flows/"):
                    flow_id = unquote(remove_prefix(path, "/api/flows/"))
                    flow = store.get_flow(flow_id, include_draft=self.has_admin_access())
                    if flow is None:
                        self.send_error_json(HTTPStatus.NOT_FOUND, "Evaluation workflow not found.")
                    else:
                        self.send_json({"flow": flow})
                elif path == "/api/submissions":
                    if not self.require_admin():
                        return
                    include_hidden = first(query, "include_hidden") == "1"
                    self.send_json(
                        {
                            "submissions": store.list_submissions(
                                first(query, "flow_id"),
                                include_hidden=include_hidden,
                            )
                        }
                    )
                elif path == "/api/submissions/export.csv":
                    if not self.require_admin():
                        return
                    csv_text = store.export_submissions_csv(first(query, "flow_id"))
                    self.send_bytes(
                        csv_text.encode("utf-8-sig"),
                        "text/csv; charset=utf-8",
                        extra_headers={"Content-Disposition": 'attachment; filename="human-eval-platform-results.csv"'},
                    )
                elif path.startswith("/videos/"):
                    self.serve_video(remove_prefix(path, "/videos/"))
                elif path.startswith("/video-preview-assets/"):
                    if self.require_evaluation_media_access() is None and governance.settings.enabled:
                        return
                    self.serve_video_preview_asset(remove_prefix(path, "/video-preview-assets/"))
                else:
                    self.serve_static(path)
            except ValidationError as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                self.close_connection = True
            except Exception as exc:  # pragma: no cover
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Internal server error: {exc}")

        def do_POST(self) -> None:
            self._request_principal_resolved = False
            self._request_principal = None
            parsed = urlparse(self.path)
            path = parsed.path
            if not self.check_request_rate(path):
                return
            try:
                payload = self.read_json_body()
                if path == "/api/participant-session":
                    display_name = participant_name_from_payload(payload)
                    allowed, listed, canonical_name = governance.participant_allowed(
                        display_name
                    )
                    if not allowed:
                        allowlist_status = governance.allowlist.status()
                        status = (
                            HTTPStatus.SERVICE_UNAVAILABLE
                            if not allowlist_status["entry_count"]
                            else HTTPStatus.FORBIDDEN
                        )
                        self.send_json(
                            {
                                "error": (
                                    "The participant allowlist is temporarily unavailable. Contact an administrator."
                                    if status == HTTPStatus.SERVICE_UNAVAILABLE
                                    else "This identifier is not on the participant allowlist. Contact an administrator."
                                ),
                                "code": (
                                    "participant_allowlist_unavailable"
                                    if status == HTTPStatus.SERVICE_UNAVAILABLE
                                    else "participant_not_allowed"
                                ),
                            },
                            status=status,
                        )
                        return
                    submission = store.get_or_create_participant_submission(payload)
                    existing_token = self.cookie_value(PARTICIPANT_COOKIE_NAME)
                    governance.revoke_session(existing_token)
                    token, principal = governance.create_session(
                        principal_type="participant",
                        flow_id=str(submission["flow_id"]),
                        participant_key=str(submission.get("participant_key", "")),
                        canonical_participant_name=canonical_name,
                        submission_id=str(submission["id"]),
                        client_ip=self.client_key(),
                        user_agent=self.headers.get("User-Agent", ""),
                    )
                    self.send_json(
                        {
                            "submission": submission,
                            "participant_key": submission.get("participant_key"),
                            "usage": governance.policy_for(principal),
                            "allowlist_observed": listed,
                        },
                        extra_headers={
                            "Set-Cookie": self.session_cookie_header(
                                PARTICIPANT_COOKIE_NAME,
                                token,
                            )
                        },
                    )
                elif path == "/api/participant-session/logout":
                    token = self.cookie_value(PARTICIPANT_COOKIE_NAME)
                    governance.revoke_session(token)
                    self.send_json(
                        {"ok": True},
                        extra_headers={
                            "Set-Cookie": self.expired_cookie_header(
                                PARTICIPANT_COOKIE_NAME
                            )
                        },
                    )
                elif path == "/api/usage/page-event":
                    principal = self.require_participant_session(
                        require_allowlisted=False
                    )
                    if principal is None:
                        return
                    page_instance_id = str(payload.get("page_instance_id", "")).strip()
                    navigation_type = str(
                        payload.get("navigation_type", "navigate")
                    ).strip()
                    if (
                        not page_instance_id
                        or len(page_instance_id) > 128
                        or navigation_type not in {"navigate", "reload", "back_forward", "prerender"}
                    ):
                        raise ValidationError("Invalid page usage event")
                    recorded, usage = governance.record_page_event(
                        principal,
                        page_instance_id,
                        navigation_type,
                    )
                    self.send_json({"recorded": recorded, "usage": usage})
                elif path == "/api/submissions/draft":
                    if governance.settings.enabled and not self.require_submission_session(
                        payload
                    ):
                        return
                    submission = store.save_submission_progress(payload)
                    principal = self.participant_principal()
                    self.send_json(
                        {
                            "submission": submission,
                            "participant_key": submission.get("participant_key"),
                            "usage": (
                                governance.policy_for(principal)
                                if principal is not None
                                else None
                            ),
                        }
                    )
                elif path == "/api/flows":
                    if not self.require_admin():
                        return
                    flow = store.save_flow(payload, status=str(payload.get("status", "draft")))
                    self.send_json({"flow": flow})
                elif path.startswith("/api/flows/") and path.endswith("/publish"):
                    if not self.require_admin():
                        return
                    flow_id = unquote(remove_suffix(remove_prefix(path, "/api/flows/"), "/publish"))
                    flow = store.publish_flow(flow_id)
                    self.send_json({"flow": flow})
                elif path.startswith("/api/submissions/") and path.endswith("/admin-flags"):
                    if not self.require_admin():
                        return
                    submission_id = unquote(remove_suffix(remove_prefix(path, "/api/submissions/"), "/admin-flags"))
                    if not submission_id:
                        self.send_error_json(HTTPStatus.BAD_REQUEST, "Submission id is required.")
                        return
                    submission = store.update_submission_admin_flags(
                        submission_id,
                        is_pinned=optional_boolean(payload, "is_pinned"),
                        is_hidden=optional_boolean(payload, "is_hidden"),
                    )
                    if submission is None:
                        self.send_error_json(HTTPStatus.NOT_FOUND, "Submission not found.")
                    else:
                        self.send_json({"submission": submission})
                elif path.startswith("/api/submissions/") and path.endswith("/answer-review"):
                    if not self.require_admin():
                        return
                    submission_id = unquote(
                        remove_suffix(remove_prefix(path, "/api/submissions/"), "/answer-review")
                    )
                    if not submission_id:
                        self.send_error_json(HTTPStatus.BAD_REQUEST, "Submission id is required.")
                        return
                    submission = store.mark_submission_answer_for_revision(
                        submission_id,
                        str(payload.get("answer_key", "")),
                        str(payload.get("comment", "")),
                    )
                    if submission is None:
                        self.send_error_json(HTTPStatus.NOT_FOUND, "Submission not found.")
                    else:
                        self.send_json({"submission": submission})
                elif path.startswith("/api/admin/traffic/alerts/") and path.endswith(
                    "/acknowledge"
                ):
                    if not self.require_admin():
                        return
                    alert_id = unquote(
                        remove_suffix(
                            remove_prefix(path, "/api/admin/traffic/alerts/"),
                            "/acknowledge",
                        )
                    )
                    if not alert_id:
                        raise ValidationError("Alert id is required")
                    acknowledged = store.acknowledge_traffic_alert(alert_id)
                    self.send_json({"acknowledged": acknowledged})
                elif path == "/api/submissions":
                    if governance.settings.enabled and not self.require_submission_session(
                        payload
                    ):
                        return
                    submission = store.create_submission(payload)
                    principal = self.participant_principal()
                    self.send_json(
                        {
                            "submission": submission,
                            "participant_key": submission.get("participant_key"),
                            "usage": (
                                governance.policy_for(principal)
                                if principal is not None
                                else None
                            ),
                        },
                        status=HTTPStatus.CREATED,
                    )
                else:
                    self.send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found.")
            except ValidationError as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            except json.JSONDecodeError:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "Request body is not valid JSON.")
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                self.close_connection = True
            except Exception as exc:  # pragma: no cover
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Internal server error: {exc}")

        def client_key(self) -> str:
            if getattr(self, "client_address", None):
                return str(self.client_address[0])
            return "unknown"

        def check_request_rate(self, path: str) -> bool:
            allowed, retry_after = traffic_guard.allow_request(path, self.client_key())
            if allowed:
                return True
            self.send_error_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                "Too many requests. Please try again shortly.",
                extra_headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )
            return False

        def read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 2_000_000:
                raise ValidationError("Request body is too large")
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8") if raw else "{}")
            if not isinstance(data, dict):
                raise ValidationError("Request body must be a JSON object")
            return data

        def cookie_value(self, name: str) -> str | None:
            raw_cookie = self.headers.get("Cookie", "")
            if not raw_cookie:
                return None
            cookie = SimpleCookie()
            try:
                cookie.load(raw_cookie)
            except Exception:
                return None
            morsel = cookie.get(name)
            return morsel.value if morsel else None

        def participant_principal(self) -> RequestPrincipal | None:
            return governance.resolve_session(
                self.cookie_value(PARTICIPANT_COOKIE_NAME),
                expected_type="participant",
            )

        def admin_principal(self) -> RequestPrincipal | None:
            return governance.resolve_session(
                self.cookie_value(ADMIN_COOKIE_NAME),
                expected_type="admin",
            )

        def request_principal(self) -> RequestPrincipal | None:
            if getattr(self, "_request_principal_resolved", False):
                return getattr(self, "_request_principal", None)
            principal = self.admin_principal() or self.participant_principal()
            self._request_principal = principal
            self._request_principal_resolved = True
            return principal

        def require_participant_session(
            self,
            *,
            require_allowlisted: bool,
        ) -> RequestPrincipal | None:
            principal = self.participant_principal()
            if principal is None:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "Participant sign-in is required.")
                return None
            if (
                require_allowlisted
                and not governance.participant_session_is_allowed(principal)
            ):
                self.send_json(
                    {
                        "error": "This identifier is no longer on the participant allowlist. Contact an administrator.",
                        "code": "participant_not_allowed",
                    },
                    status=HTTPStatus.FORBIDDEN,
                )
                return None
            return principal

        def require_submission_session(self, payload: dict) -> bool:
            principal = self.require_participant_session(require_allowlisted=False)
            if principal is None:
                return False
            payload_name = canonicalize_participant_name(
                participant_name_from_payload(payload)
            )
            if (
                str(payload.get("flow_id", "")).strip() != principal.flow_id
                or payload_name != principal.canonical_participant_name
            ):
                self.send_error_json(
                    HTTPStatus.FORBIDDEN,
                    "The participant session does not match the submission. Please sign in again.",
                )
                return False
            return True

        def require_evaluation_media_access(self) -> RequestPrincipal | None:
            principal = self.request_principal()
            if not governance.settings.enabled:
                return principal
            if principal is None:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "Sign in before accessing evaluation media.")
                return None
            if principal.principal_type == "admin":
                return principal
            if not governance.participant_session_is_allowed(principal):
                governance.record_rejected_request(principal)
                self.send_json(
                    {
                        "error": "This identifier is no longer on the participant allowlist. Contact an administrator.",
                        "code": "participant_not_allowed",
                    },
                    status=HTTPStatus.FORBIDDEN,
                )
                return None
            policy = governance.policy_for(principal)
            if policy["blocked"]:
                governance.record_rejected_request(principal)
                self.send_json(
                    {
                        "error": "Evaluation access is paused for today. Existing input will still be saved.",
                        "code": "participant_daily_blocked",
                        "usage": policy,
                    },
                    status=HTTPStatus.TOO_MANY_REQUESTS,
                    extra_headers={
                        "Retry-After": str(retry_after_seconds(policy.get("reset_at")))
                    },
                )
                return None
            return principal

        def session_cookie_header(self, name: str, token: str) -> str:
            parts = [
                f"{name}={token}",
                "Path=/",
                "HttpOnly",
                "SameSite=Lax",
                f"Max-Age={governance.settings.session_ttl_seconds}",
            ]
            if governance.settings.session_cookie_secure:
                parts.append("Secure")
            return "; ".join(parts)

        def expired_cookie_header(self, name: str) -> str:
            parts = [
                f"{name}=",
                "Path=/",
                "HttpOnly",
                "SameSite=Lax",
                "Max-Age=0",
            ]
            if governance.settings.session_cookie_secure:
                parts.append("Secure")
            return "; ".join(parts)

        def ensure_admin_session(self) -> tuple[str | None, RequestPrincipal | None]:
            existing = self.admin_principal()
            if existing is not None:
                return None, existing
            if self.headers.get("X-Admin-Password", "") != ADMIN_PASSWORD:
                return None, None
            return governance.create_session(
                principal_type="admin",
                client_ip=self.client_key(),
                user_agent=self.headers.get("User-Agent", ""),
            )

        def serve_static(self, path: str) -> None:
            relative = "index.html" if path in {"/", ""} else path.lstrip("/")
            if relative.startswith("static/"):
                relative = remove_prefix(relative, "static/")
            file_path = safe_join(config.static_dir, relative)
            if file_path is None or not file_path.is_file():
                file_path = config.static_dir / "index.html"
            content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            self.send_bytes(file_path.read_bytes(), content_type)

        def serve_video(self, relative_path: str) -> None:
            principal = self.require_evaluation_media_access()
            if governance.settings.enabled and principal is None:
                return
            file_path = find_video_file(config, relative_path)
            if file_path is None:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Video file not found.")
                return
            content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            file_stat = file_path.stat()
            file_size = file_stat.st_size
            etag = file_etag(file_stat)
            last_modified = formatdate(file_stat.st_mtime, usegmt=True)
            cache_control = video_cache_control(
                video_cache_max_age,
                private=governance.settings.enabled,
            )

            if request_is_not_modified(self.headers, etag, file_stat.st_mtime):
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_video_cache_headers(etag, last_modified, cache_control)
                self.end_headers()
                return

            range_header = self.headers.get("Range")
            if range_header and not if_range_allows_range(
                self.headers.get("If-Range"),
                etag,
                file_stat.st_mtime,
            ):
                range_header = None

            status = HTTPStatus.OK
            start = 0
            end = file_size - 1
            if range_header:
                start, end = parse_range_header(range_header, file_size)
                if start is None:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_video_cache_headers(etag, last_modified, cache_control)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT

            if principal is not None and principal.principal_type == "participant":
                policy = governance.policy_for(principal)
                allowed_bytes = int(policy.get("remaining_bytes", end - start + 1))
                content_length = max(0, end - start + 1)
                if 0 < allowed_bytes < content_length:
                    end = start + allowed_bytes - 1
                    status = HTTPStatus.PARTIAL_CONTENT

            if principal is not None:
                governance.record_media_request(
                    principal,
                    is_range=status == HTTPStatus.PARTIAL_CONTENT,
                )

            if self.command == "HEAD":
                self.send_video_response_headers(
                    status,
                    content_type,
                    file_size,
                    start,
                    end,
                    etag,
                    last_modified,
                    cache_control,
                )
                return

            client_key = self.client_key()
            acquired, reason = traffic_guard.acquire_video(client_key)
            if not acquired:
                status_code = (
                    HTTPStatus.TOO_MANY_REQUESTS
                    if reason == "per_ip"
                    else HTTPStatus.SERVICE_UNAVAILABLE
                )
                self.send_error_json(
                    status_code,
                    "Video service is busy. Please try again shortly.",
                    extra_headers={"Retry-After": "2"},
                )
                return

            try:
                self.send_video_response_headers(
                    status,
                    content_type,
                    file_size,
                    start,
                    end,
                    etag,
                    last_modified,
                    cache_control,
                )
                self.stream_file_range(
                    file_path,
                    start,
                    end,
                    client_key,
                    principal,
                )
            finally:
                traffic_guard.release_video(client_key)

        def send_video_response_headers(
            self,
            status: HTTPStatus,
            content_type: str,
            file_size: int,
            start: int,
            end: int,
            etag: str,
            last_modified: str,
            cache_control: str,
        ) -> None:
            content_length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_video_cache_headers(etag, last_modified, cache_control)
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Content-Length", str(content_length))
            self.end_headers()

        def send_video_cache_headers(
            self,
            etag: str,
            last_modified: str,
            cache_control: str,
        ) -> None:
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", cache_control)
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_modified)
            self.send_header("X-Content-Type-Options", "nosniff")
            if governance.settings.enabled:
                self.send_header("Vary", "Cookie")

        def stream_file_range(
            self,
            file_path: Path,
            start: int,
            end: int,
            client_key: str,
            principal: RequestPrincipal | None,
        ) -> None:
            remaining = max(0, end - start + 1)
            with file_path.open("rb") as handle:
                handle.seek(start)
                while remaining > 0:
                    participant_rate = None
                    participant_bandwidth_key = None
                    if principal is not None and principal.principal_type == "participant":
                        policy = governance.policy_for(principal)
                        if policy["blocked"]:
                            governance.record_rejected_request(principal)
                            break
                        participant_rate = governance.video_rate_bytes_per_second(
                            principal,
                            protection.video_bandwidth_mib_per_second_per_ip * MIB,
                        )
                        participant_bandwidth_key = (
                            f"{principal.flow_id}:{principal.participant_key}"
                        )
                    chunk = handle.read(min(VIDEO_STREAM_CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    traffic_guard.wait_for_video_bandwidth(
                        client_key,
                        len(chunk),
                        participant_key=participant_bandwidth_key,
                        participant_rate=participant_rate,
                    )
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, TimeoutError):
                        self.close_connection = True
                        break
                    if principal is not None:
                        governance.record_egress(principal, len(chunk), "video")
                    remaining -= len(chunk)

        def serve_video_text(
            self,
            video_path: str | None,
            text_path: str | None = None,
            language: str | None = None,
        ) -> None:
            if not video_path:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "video_path is required.")
                return
            source_path = find_source_video_text_file(config, video_path, text_path)
            if source_path is None:
                self.send_error_json(
                    HTTPStatus.NOT_FOUND,
                    "Matching transcript not found. Place the text file beside the video and use a related filename.",
                )
                return
            translation_path = find_translation_file(source_path)
            normalized_language = normalize_text_language(language)
            file_path = translation_path if normalized_language == "translation" else source_path
            if file_path is None:
                normalized_language = "original"
                file_path = source_path
            self.send_json(
                {
                    "path": display_relative_path(config, file_path),
                    "sourcePath": display_relative_path(config, source_path),
                    "language": normalized_language,
                    "translationAvailable": translation_path is not None,
                    "text": read_text_file(file_path),
                }
            )

        def serve_video_preview(self, video_path: str | None) -> None:
            if not video_path:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "video_path is required.")
                return
            if find_video_file(config, video_path) is None:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Video file not found.")
                return
            manifest_path = find_video_preview_manifest(config, video_path)
            if manifest_path is None:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Video preview not found.")
                return
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValidationError("Invalid video preview manifest") from exc
            if not isinstance(manifest, dict):
                raise ValidationError("Invalid video preview manifest")
            if normalize_video_preview_path(str(manifest.get("videoPath", ""))) != normalize_video_preview_path(
                video_path
            ):
                raise ValidationError("Video preview manifest does not match the requested video")
            manifest["assetsBasePath"] = f"/video-preview-assets/{video_preview_id(video_path)}/"
            self.send_json(manifest)

        def serve_video_preview_asset(self, relative_path: str) -> None:
            file_path = find_video_preview_asset(config, relative_path)
            if file_path is None:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Video preview asset not found.")
                return
            content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            self.send_file(
                file_path,
                content_type,
                file_path.stat().st_size,
                cache_control=(
                    "private, max-age=31536000, immutable"
                    if governance.settings.enabled
                    else "public, max-age=31536000, immutable"
                ),
            )

        def send_file(
            self,
            file_path: Path,
            content_type: str,
            file_size: int,
            cache_control: str = "no-store",
        ) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", cache_control)
            if governance.settings.enabled:
                self.send_header("Vary", "Cookie")
            self.end_headers()
            if self.command == "HEAD":
                return
            with file_path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, TimeoutError):
                        self.close_connection = True
                        break
                    principal = self.request_principal()
                    if principal is not None:
                        governance.record_egress(principal, len(chunk), "preview")

        def send_json(
            self,
            payload: dict,
            status: HTTPStatus = HTTPStatus.OK,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.send_bytes(
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                status=status,
                extra_headers=extra_headers,
            )

        def send_error_json(
            self,
            status: HTTPStatus,
            message: str,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.send_bytes(
                json.dumps({"error": message}, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                status=status,
                extra_headers=extra_headers,
            )

        def has_admin_access(self) -> bool:
            return (
                bool(ADMIN_PASSWORD)
                and self.headers.get("X-Admin-Password", "") == ADMIN_PASSWORD
                or self.admin_principal() is not None
            )

        def require_admin(self) -> bool:
            if self.has_admin_access():
                return True
            self.send_error_json(HTTPStatus.FORBIDDEN, "Administrator password required.")
            return False

        def send_bytes(
            self,
            payload: bytes,
            content_type: str,
            status: HTTPStatus = HTTPStatus.OK,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
                principal = self.request_principal()
                if principal is not None and payload:
                    governance.record_egress(
                        principal,
                        len(payload),
                        self.response_usage_category(),
                    )

        def response_usage_category(self) -> str:
            path = urlparse(self.path).path
            if path == "/api/video-text":
                return "text"
            if path == "/api/video-preview" or path.startswith(
                "/video-preview-assets/"
            ):
                return "preview"
            return "api_static"

        def log_message(self, format: str, *args) -> None:
            if os.environ.get("HEP_DEBUG"):
                super().log_message(format, *args)

    return HumanEvalHandler


def file_etag(file_stat: os.stat_result) -> str:
    return f'"{file_stat.st_mtime_ns:x}-{file_stat.st_size:x}"'


def video_cache_control(max_age_seconds: int, private: bool = False) -> str:
    max_age = max(0, int(max_age_seconds))
    visibility = "private" if private else "public"
    if max_age == 0:
        return f"{visibility}, max-age=0, must-revalidate"
    return f"{visibility}, max-age={max_age}, immutable"


def retry_after_seconds(reset_at: Any) -> int:
    try:
        reset = datetime.fromisoformat(str(reset_at))
        if reset.tzinfo is None:
            reset = reset.replace(tzinfo=timezone.utc)
        return max(1, math.ceil((reset - datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return 60


def parse_http_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def normalize_etag(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:].strip()
    return normalized


def request_is_not_modified(headers, etag: str, modified_at: float) -> bool:
    if_none_match = headers.get("If-None-Match")
    if if_none_match is not None:
        candidates = [item.strip() for item in if_none_match.split(",")]
        return "*" in candidates or any(normalize_etag(item) == etag for item in candidates)
    modified_since = parse_http_date(headers.get("If-Modified-Since"))
    return modified_since is not None and int(modified_at) <= int(modified_since)


def if_range_allows_range(if_range: str | None, etag: str, modified_at: float) -> bool:
    if not if_range:
        return True
    candidate = if_range.strip()
    if candidate.startswith("W/"):
        return False
    if candidate.startswith('"'):
        return candidate == etag
    range_date = parse_http_date(candidate)
    return range_date is not None and int(modified_at) <= int(range_date)


def first(query: dict[str, list[str]], key: str) -> str | None:
    value = query.get(key)
    if not value:
        return None
    return value[0]


def optional_boolean(payload: dict, key: str) -> bool | None:
    if key not in payload:
        return None
    value = payload[key]
    if not isinstance(value, bool):
        raise ValidationError(f"{key} must be boolean")
    return value


def safe_join(root: Path, relative_path: str) -> Path | None:
    candidate = (root / unquote(relative_path)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def find_video_file(config: AppConfig, relative_path: str) -> Path | None:
    roots = (config.video_dir, *config.extra_video_dirs)
    for root in roots:
        candidate = safe_join(root, relative_path)
        if candidate is not None and candidate.is_file():
            return candidate
    return None


def normalize_video_preview_path(relative_path: str) -> str:
    return unquote(str(relative_path or "")).replace("\\", "/").lstrip("/")


def video_preview_id(relative_path: str) -> str:
    normalized = normalize_video_preview_path(relative_path)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def find_video_preview_manifest(config: AppConfig, video_path: str) -> Path | None:
    if config.video_preview_dir is None:
        return None
    manifest = safe_join(config.video_preview_dir, f"{video_preview_id(video_path)}/manifest.json")
    return manifest if manifest is not None and manifest.is_file() else None


def find_video_preview_asset(config: AppConfig, relative_path: str) -> Path | None:
    if config.video_preview_dir is None:
        return None
    asset = safe_join(config.video_preview_dir, relative_path)
    if asset is None or not asset.is_file() or asset.suffix.lower() not in {".jpg", ".jpeg", ".webp", ".png"}:
        return None
    return asset


def find_video_text_file(
    config: AppConfig,
    video_path: str,
    text_path: str | None = None,
    language: str | None = None,
) -> Path | None:
    source_path = find_source_video_text_file(config, video_path, text_path)
    if source_path is None:
        return None
    if normalize_text_language(language) == "translation":
        return find_translation_file(source_path)
    return source_path


def find_source_video_text_file(config: AppConfig, video_path: str, text_path: str | None = None) -> Path | None:
    if text_path:
        configured = find_video_file(config, text_path)
        if configured is not None and configured.suffix.lower() == ".txt":
            return configured

    video_file = find_video_file(config, video_path)
    if video_file is None:
        return None

    exact = video_file.with_suffix(".txt")
    if exact.is_file():
        return exact

    try:
        candidates = sorted(
            item
            for item in video_file.parent.iterdir()
            if item.is_file() and item.suffix.lower() == ".txt" and not is_translation_file(item)
        )
    except OSError:
        return None
    return best_text_candidate(video_file, candidates)


def normalize_text_language(language: str | None) -> str:
    value = str(language or "original").strip().lower()
    return "translation" if value in {"translation", "translated"} else "original"


def find_translation_file(source_path: Path) -> Path | None:
    candidates = [
        source_path.with_name(f"{source_path.stem}.translation{source_path.suffix}"),
        source_path.with_name(f"{source_path.stem}_translation{source_path.suffix}"),
        source_path.with_name(f"{source_path.stem}__translation{source_path.suffix}"),
        source_path.with_name(f"{source_path.stem}.translated{source_path.suffix}"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def is_translation_file(path: Path) -> bool:
    stem = path.stem.lower()
    return stem.endswith((".translation", "_translation", "__translation", ".translated"))


NAME_STOPWORDS = {
    "full",
    "novel",
    "original",
    "subtitle",
    "subtitles",
    "text",
    "txt",
    "video",
    "with",
}


def best_text_candidate(video_file: Path, candidates: list[Path]) -> Path | None:
    if not candidates:
        return None
    scored = [(name_similarity_score(video_file.stem, candidate.stem), candidate) for candidate in candidates]
    score, candidate = max(scored, key=lambda item: (item[0], -len(item[1].name)))
    minimum_score = 25 if len(candidates) == 1 else 55
    return candidate if score >= minimum_score else None


def name_similarity_score(video_stem: str, text_stem: str) -> int:
    video_tokens = meaningful_name_tokens(video_stem)
    text_tokens = meaningful_name_tokens(text_stem)
    video_compact = "".join(video_tokens)
    text_compact = "".join(text_tokens)
    score = 0

    if video_compact and text_compact:
        if video_compact == text_compact:
            score += 1000
        elif video_compact in text_compact or text_compact in video_compact:
            score += 200 + min(len(video_compact), len(text_compact)) * 3
        common_length = longest_common_substring_length(video_compact, text_compact)
        if common_length >= 2:
            score += common_length * 4

    overlap = set(video_tokens) & set(text_tokens)
    score += sum(len(token) * 6 for token in overlap)
    if video_tokens and text_tokens and video_tokens[0].isdigit() and video_tokens[0] == text_tokens[0]:
        score += 20
    return score


def meaningful_name_tokens(stem: str) -> list[str]:
    tokens = re.findall(r"[^\W_]+", stem.lower(), flags=re.UNICODE)
    meaningful = [token for token in tokens if token not in NAME_STOPWORDS]
    return meaningful or tokens


def longest_common_substring_length(left: str, right: str) -> int:
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    best = 0
    for left_char in left:
        current = [0]
        for index, right_char in enumerate(right, start=1):
            value = previous[index - 1] + 1 if left_char == right_char else 0
            current.append(value)
            best = max(best, value)
        previous = current
    return best


def read_text_file(file_path: Path) -> str:
    data = file_path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def display_relative_path(config: AppConfig, file_path: Path) -> str:
    resolved = file_path.resolve()
    for root in (config.video_dir, *config.extra_video_dirs):
        try:
            return str(resolved.relative_to(root.resolve()))
        except ValueError:
            continue
    return file_path.name


def remove_prefix(value: str, prefix: str) -> str:
    if value.startswith(prefix):
        return value[len(prefix) :]
    return value


def remove_suffix(value: str, suffix: str) -> str:
    if suffix and value.endswith(suffix):
        return value[: -len(suffix)]
    return value


def parse_range_header(header: str, file_size: int) -> tuple[int | None, int]:
    if not header.startswith("bytes="):
        return None, 0
    value = remove_prefix(header, "bytes=")
    start_text, _, end_text = value.partition("-")
    try:
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
        else:
            suffix = int(end_text)
            start = max(file_size - suffix, 0)
            end = file_size - 1
    except ValueError:
        return None, 0
    if start < 0 or start >= file_size or end < start:
        return None, 0
    return start, min(end, file_size - 1)


def run(config: AppConfig) -> None:
    store = EvaluationStore(config)
    store.initialize()
    governance = ParticipantGovernance(config, store)
    handler = create_handler(config, store, governance)
    max_connections = (
        config.traffic_protection.max_http_connections_total
        if config.traffic_protection.enabled
        else 0
    )
    httpd = LimitedThreadingHTTPServer(
        (config.host, config.port),
        handler,
        max_connections=max_connections,
    )
    print(f"Human Eval Platform started: http://{config.host}:{config.port}")
    print(f"Video directory: {config.video_dir}")
    if not ADMIN_PASSWORD:
        print("Administration is disabled. Set HEP_ADMIN_PASSWORD to enable it.")
    governance.start()
    try:
        httpd.serve_forever()
    finally:
        governance.stop()
        httpd.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Start Human Eval Platform.")
    parser.add_argument("--config", help="Path to a JSON configuration file.", default=None)
    args = parser.parse_args()
    run(load_config(args.config))
