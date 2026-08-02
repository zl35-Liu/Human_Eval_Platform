from __future__ import annotations

import hashlib
import math
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

from .config import AppConfig, ParticipantGovernanceConfig


DECIMAL_GB = 1_000_000_000
PARTICIPANT_COOKIE_NAME = "hep_participant_session"
ADMIN_COOKIE_NAME = "hep_admin_session"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def policy_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def next_policy_reset() -> str:
    now = datetime.now(timezone.utc)
    tomorrow = now.date() + timedelta(days=1)
    return datetime.combine(tomorrow, datetime.min.time(), tzinfo=timezone.utc).isoformat()


def canonicalize_participant_name(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or "").strip()).casefold()


def participant_name_from_payload(payload: dict[str, Any]) -> str:
    participant = payload.get("participant")
    if not isinstance(participant, dict):
        return ""
    for field_id in (
        "participant_code",
        "participant_name",
        "subject_code",
        "subject_name",
        "name",
        "username",
    ):
        value = str(participant.get(field_id, "")).strip()
        if value:
            return value
    return ""


@dataclass(frozen=True)
class AllowlistSnapshot:
    canonical_names: frozenset[str]
    display_names: dict[str, str]
    content_hash: str
    loaded_at: str
    file_signature: tuple[int, int]


class ParticipantAllowlist:
    def __init__(
        self,
        path: Path,
        reload_seconds: int = 10,
        maximum_name_length: int = 200,
    ) -> None:
        self.path = path
        self.reload_seconds = max(1, int(reload_seconds))
        self.maximum_name_length = maximum_name_length
        self._lock = threading.RLock()
        self._snapshot: AllowlistSnapshot | None = None
        self._last_error = ""
        self._last_checked_monotonic = 0.0
        self.reload(force=True)

    def reload(self, force: bool = False) -> AllowlistSnapshot | None:
        current = time.monotonic()
        with self._lock:
            if (
                not force
                and current - self._last_checked_monotonic < self.reload_seconds
            ):
                return self._snapshot
            self._last_checked_monotonic = current

        try:
            before = self.path.stat()
            signature = (int(before.st_mtime_ns), int(before.st_size))
            with self._lock:
                if (
                    not force
                    and self._snapshot is not None
                    and self._snapshot.file_signature == signature
                ):
                    return self._snapshot
            content = self.path.read_text(encoding="utf-8")
            after = self.path.stat()
            after_signature = (int(after.st_mtime_ns), int(after.st_size))
            if signature != after_signature:
                raise ValueError("The allowlist changed while it was being read")
            display_names = parse_allowlist_document(
                content,
                maximum_name_length=self.maximum_name_length,
            )
            snapshot = AllowlistSnapshot(
                canonical_names=frozenset(display_names),
                display_names=display_names,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                loaded_at=utc_now().replace(microsecond=0).isoformat(),
                file_signature=signature,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            with self._lock:
                self._last_error = str(exc)
                return self._snapshot

        with self._lock:
            self._snapshot = snapshot
            self._last_error = ""
            return self._snapshot

    def snapshot(self) -> AllowlistSnapshot | None:
        return self.reload(force=False)

    def is_allowed(self, name: Any) -> tuple[bool, str]:
        canonical_name = canonicalize_participant_name(name)
        snapshot = self.snapshot()
        return (
            bool(snapshot and canonical_name in snapshot.canonical_names),
            canonical_name,
        )

    def status(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        with self._lock:
            return {
                "healthy": snapshot is not None and not self._last_error,
                "entry_count": len(snapshot.canonical_names) if snapshot else 0,
                "active_hash": snapshot.content_hash if snapshot else "",
                "last_loaded_at": snapshot.loaded_at if snapshot else None,
                "last_error": self._last_error or None,
            }


def parse_allowlist_document(
    content: str,
    maximum_name_length: int = 200,
) -> dict[str, str]:
    lines = content.lstrip("\ufeff").splitlines()
    heading_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip() == "## Identifiers"
        ),
        None,
    )
    if heading_index is None:
        raise ValueError("The allowlist is missing the '## Identifiers' heading")

    fence_index = next(
        (
            index
            for index in range(heading_index + 1, len(lines))
            if lines[index].strip() == "```text"
        ),
        None,
    )
    if fence_index is None:
        raise ValueError("The allowlist is missing a text code block")
    end_index = next(
        (
            index
            for index in range(fence_index + 1, len(lines))
            if lines[index].strip() == "```"
        ),
        None,
    )
    if end_index is None:
        raise ValueError("The allowlist text code block is not closed")

    display_names: dict[str, str] = {}
    for raw_name in lines[fence_index + 1 : end_index]:
        display_name = unicodedata.normalize("NFC", raw_name.strip())
        if not display_name:
            continue
        if len(display_name) > maximum_name_length:
            raise ValueError("The allowlist contains an overlong identifier")
        if any(unicodedata.category(char) == "Cc" for char in display_name):
            raise ValueError("The allowlist contains a control character")
        canonical_name = canonicalize_participant_name(display_name)
        if canonical_name in display_names:
            raise ValueError(
                f"The allowlist contains a duplicate normalized identifier: {display_name}"
            )
        display_names[canonical_name] = display_name
    if not display_names:
        raise ValueError("The allowlist contains no participant identifiers")
    return display_names


@dataclass(frozen=True)
class RequestPrincipal:
    session_id: str
    token_hash: str
    principal_type: str
    flow_id: str
    participant_key: str
    canonical_participant_name: str
    submission_id: str
    expires_at_epoch: float


class ParticipantGovernance:
    def __init__(self, config: AppConfig, store: Any) -> None:
        self.config = config
        self.settings: ParticipantGovernanceConfig = config.participant_governance
        allowlist_path = config.participant_allowlist_path
        if allowlist_path is None:
            allowlist_path = Path("docs/participant-allowlist.md").resolve()
        self.allowlist = ParticipantAllowlist(
            allowlist_path,
            reload_seconds=self.settings.allowlist_reload_seconds,
        )
        self.store = store
        self._lock = threading.RLock()
        self._states: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._pending: dict[tuple[str, str, str], dict[str, int]] = {}
        self._session_cache: dict[str, RequestPrincipal] = {}
        self._stop_event = threading.Event()
        self._flush_thread: threading.Thread | None = None
        self._audit_salt = secrets.token_bytes(32)

    @property
    def enforcing(self) -> bool:
        return self.settings.enabled and self.settings.enforcement_mode == "enforce"

    def start(self) -> None:
        if self._flush_thread is not None:
            return
        self._flush_thread = threading.Thread(
            target=self._flush_worker,
            name="participant-usage-flush",
            daemon=True,
        )
        self._flush_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._flush_thread is not None:
            self._flush_thread.join(timeout=max(2, self.settings.usage_flush_seconds + 1))
        self.flush()

    def _flush_worker(self) -> None:
        while not self._stop_event.wait(self.settings.usage_flush_seconds):
            self.flush()

    def hash_audit_value(self, value: str) -> str:
        return hashlib.sha256(self._audit_salt + value.encode("utf-8")).hexdigest()

    def participant_allowed(self, name: Any) -> tuple[bool, bool, str]:
        listed, canonical_name = self.allowlist.is_allowed(name)
        allowed = listed or not self.enforcing
        return allowed, listed, canonical_name

    def create_session(
        self,
        *,
        principal_type: str,
        flow_id: str = "",
        participant_key: str = "",
        canonical_participant_name: str = "",
        submission_id: str = "",
        client_ip: str = "",
        user_agent: str = "",
    ) -> tuple[str, RequestPrincipal]:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = utc_now()
        expires_at = now + timedelta(seconds=self.settings.session_ttl_seconds)
        allowlist_snapshot = self.allowlist.snapshot()
        record = self.store.create_principal_session(
            {
                "token_hash": token_hash,
                "principal_type": principal_type,
                "flow_id": flow_id,
                "participant_key": participant_key,
                "canonical_participant_name": canonical_participant_name,
                "submission_id": submission_id,
                "created_at": now.replace(microsecond=0).isoformat(),
                "expires_at": expires_at.replace(microsecond=0).isoformat(),
                "last_seen_at": now.replace(microsecond=0).isoformat(),
                "last_ip_hash": self.hash_audit_value(client_ip) if client_ip else "",
                "user_agent_hash": (
                    self.hash_audit_value(user_agent) if user_agent else ""
                ),
                "allowlist_hash_at_issue": (
                    allowlist_snapshot.content_hash if allowlist_snapshot else ""
                ),
            }
        )
        principal = principal_from_session_record(record)
        with self._lock:
            self._session_cache[token_hash] = principal
        return token, principal

    def resolve_session(
        self,
        token: str | None,
        expected_type: str | None = None,
    ) -> RequestPrincipal | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now_epoch = time.time()
        with self._lock:
            cached = self._session_cache.get(token_hash)
            if cached and cached.expires_at_epoch > now_epoch:
                if expected_type is None or cached.principal_type == expected_type:
                    return cached
                return None
            self._session_cache.pop(token_hash, None)
        record = self.store.get_principal_session(token_hash)
        if not record:
            return None
        principal = principal_from_session_record(record)
        if principal.expires_at_epoch <= now_epoch:
            return None
        if expected_type is not None and principal.principal_type != expected_type:
            return None
        with self._lock:
            self._session_cache[token_hash] = principal
        return principal

    def revoke_session(self, token: str | None) -> None:
        if not token:
            return
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self.store.revoke_principal_session(token_hash)
        with self._lock:
            self._session_cache.pop(token_hash, None)

    def participant_session_is_allowed(self, principal: RequestPrincipal) -> bool:
        if principal.principal_type != "participant":
            return True
        listed, canonical_name = self.allowlist.is_allowed(
            principal.canonical_participant_name
        )
        return (
            listed
            and canonical_name == principal.canonical_participant_name
        ) or not self.enforcing

    def policy_for(self, principal: RequestPrincipal) -> dict[str, Any]:
        if principal.principal_type == "admin":
            return {
                "exempt": True,
                "blocked": False,
                "effective_factor": 1.0,
                "enforced_factor": 1.0,
                "messages": [],
            }
        state = self._state_for(principal)
        return self._policy_from_state(state)

    def _state_for(self, principal: RequestPrincipal) -> dict[str, Any]:
        local_date = policy_date()
        key = (principal.flow_id, principal.participant_key, local_date)
        with self._lock:
            existing = self._states.get(key)
            if existing is not None:
                return dict(existing)
        persisted = self.store.get_daily_traffic_usage(*key)
        manual_factor = self.store.get_participant_manual_factor(
            principal.flow_id,
            principal.participant_key,
        )
        state = {
            "flow_id": principal.flow_id,
            "participant_key": principal.participant_key,
            "local_date": local_date,
            "egress_bytes": int(persisted.get("egress_bytes", 0)),
            "video_bytes": int(persisted.get("video_bytes", 0)),
            "preview_bytes": int(persisted.get("preview_bytes", 0)),
            "text_bytes": int(persisted.get("text_bytes", 0)),
            "api_static_bytes": int(persisted.get("api_static_bytes", 0)),
            "video_request_count": int(persisted.get("video_request_count", 0)),
            "range_request_count": int(persisted.get("range_request_count", 0)),
            "rejected_request_count": int(persisted.get("rejected_request_count", 0)),
            "document_load_count": int(persisted.get("document_load_count", 0)),
            "reported_reload_count": int(persisted.get("reported_reload_count", 0)),
            "manual_factor": float(manual_factor),
        }
        with self._lock:
            current = self._states.setdefault(key, state)
            return dict(current)

    def _policy_from_state(self, state: dict[str, Any]) -> dict[str, Any]:
        traffic_index = tier_index(
            float(state.get("egress_bytes", 0)),
            tuple(value * DECIMAL_GB for value in self.settings.traffic_thresholds_gb),
        )
        refresh_index = tier_index(
            float(state.get("reported_reload_count", 0)),
            tuple(float(value) for value in self.settings.refresh_thresholds),
        )
        traffic_factor = float(self.settings.traffic_factors[traffic_index])
        refresh_factor = float(self.settings.refresh_factors[refresh_index])
        automatic_factor = traffic_factor * refresh_factor
        manual_factor = min(1.0, max(0.0, float(state.get("manual_factor", 1.0))))
        effective_factor = min(automatic_factor, manual_factor)
        blocked_by_policy = effective_factor <= 0
        blocked = blocked_by_policy and self.enforcing
        enforced_factor = effective_factor if self.enforcing else 1.0
        egress_bytes = int(state.get("egress_bytes", 0))
        hard_limit_bytes = int(self.settings.traffic_thresholds_gb[-1] * DECIMAL_GB)
        messages = policy_messages(
            traffic_index,
            refresh_index,
            traffic_factor,
            refresh_factor,
            effective_factor,
            egress_bytes,
            int(state.get("reported_reload_count", 0)),
            self.settings,
        )
        if not self.enforcing and (traffic_index or refresh_index):
            messages = [f"Observe mode: {message}" for message in messages]
        return {
            "exempt": False,
            "mode": self.settings.enforcement_mode,
            "local_date": str(state["local_date"]),
            "egress_bytes": egress_bytes,
            "egress_gb": round(egress_bytes / DECIMAL_GB, 3),
            "video_bytes": int(state.get("video_bytes", 0)),
            "preview_bytes": int(state.get("preview_bytes", 0)),
            "text_bytes": int(state.get("text_bytes", 0)),
            "api_static_bytes": int(state.get("api_static_bytes", 0)),
            "video_request_count": int(state.get("video_request_count", 0)),
            "range_request_count": int(state.get("range_request_count", 0)),
            "rejected_request_count": int(state.get("rejected_request_count", 0)),
            "document_load_count": int(state.get("document_load_count", 0)),
            "reload_count": int(state.get("reported_reload_count", 0)),
            "traffic_tier": traffic_index,
            "refresh_tier": refresh_index,
            "traffic_factor": traffic_factor,
            "refresh_factor": refresh_factor,
            "automatic_factor": automatic_factor,
            "manual_factor": manual_factor,
            "effective_factor": effective_factor,
            "enforced_factor": enforced_factor,
            "blocked": blocked,
            "would_block": blocked_by_policy,
            "remaining_bytes": max(0, hard_limit_bytes - egress_bytes),
            "reset_at": next_policy_reset(),
            "messages": messages,
        }

    def video_rate_bytes_per_second(
        self,
        principal: RequestPrincipal,
        normal_rate_bytes_per_second: float,
    ) -> float | None:
        if principal.principal_type == "admin" or not self.settings.enabled:
            return None
        policy = self.policy_for(principal)
        return normal_rate_bytes_per_second * float(policy["enforced_factor"])

    def record_media_request(
        self,
        principal: RequestPrincipal,
        *,
        is_range: bool = False,
    ) -> None:
        if principal.principal_type != "participant":
            return
        deltas = {"video_request_count": 1}
        if is_range:
            deltas["range_request_count"] = 1
        self._increment(principal, deltas)

    def record_rejected_request(self, principal: RequestPrincipal) -> None:
        if principal.principal_type == "participant":
            self._increment(principal, {"rejected_request_count": 1})

    def record_egress(
        self,
        principal: RequestPrincipal,
        amount: int,
        category: str,
    ) -> None:
        if principal.principal_type != "participant" or amount <= 0:
            return
        category_field = {
            "video": "video_bytes",
            "preview": "preview_bytes",
            "text": "text_bytes",
        }.get(category, "api_static_bytes")
        self._increment(
            principal,
            {
                "egress_bytes": int(amount),
                category_field: int(amount),
            },
        )

    def _increment(
        self,
        principal: RequestPrincipal,
        deltas: dict[str, int],
    ) -> None:
        before = self.policy_for(principal)
        local_date = policy_date()
        key = (principal.flow_id, principal.participant_key, local_date)
        with self._lock:
            state = self._states[key]
            pending = self._pending.setdefault(key, {})
            for field, amount in deltas.items():
                state[field] = int(state.get(field, 0)) + int(amount)
                pending[field] = int(pending.get(field, 0)) + int(amount)
            after_state = dict(state)
        after = self._policy_from_state(after_state)
        self._emit_tier_alerts(principal, before, after)

    def record_page_event(
        self,
        principal: RequestPrincipal,
        page_instance_id: str,
        navigation_type: str,
    ) -> tuple[bool, dict[str, Any]]:
        if principal.principal_type != "participant":
            return False, self.policy_for(principal)
        before = self.policy_for(principal)
        inserted, persisted = self.store.record_traffic_page_event(
            {
                "page_instance_id": page_instance_id,
                "session_id": principal.session_id,
                "flow_id": principal.flow_id,
                "participant_key": principal.participant_key,
                "event_type": "page_load",
                "navigation_type": navigation_type,
                "created_at": utc_now().replace(microsecond=0).isoformat(),
                "local_date": policy_date(),
            }
        )
        if inserted:
            key = (
                principal.flow_id,
                principal.participant_key,
                policy_date(),
            )
            with self._lock:
                state = self._states.get(key)
                if state is None:
                    self._states[key] = {
                        **persisted,
                        "manual_factor": self.store.get_participant_manual_factor(
                            principal.flow_id,
                            principal.participant_key,
                        ),
                    }
                else:
                    state["document_load_count"] = int(
                        persisted.get("document_load_count", 0)
                    )
                    state["reported_reload_count"] = int(
                        persisted.get("reported_reload_count", 0)
                    )
            after = self.policy_for(principal)
            self._emit_tier_alerts(principal, before, after)
        return inserted, self.policy_for(principal)

    def _emit_tier_alerts(
        self,
        principal: RequestPrincipal,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> None:
        alerts: list[tuple[str, int, int, str]] = []
        if int(after["traffic_tier"]) > int(before["traffic_tier"]):
            alerts.append(
                (
                    "traffic",
                    int(after["traffic_tier"]),
                    int(after["egress_bytes"]),
                    "; ".join(after["messages"]) or "Participant traffic reached a threshold",
                )
            )
        if int(after["refresh_tier"]) > int(before["refresh_tier"]):
            alerts.append(
                (
                    "refresh",
                    int(after["refresh_tier"]),
                    int(after["reload_count"]),
                    "; ".join(after["messages"]) or "Participant reloads reached a threshold",
                )
            )
        for alert_type, tier, observed_value, message in alerts:
            self.store.create_traffic_alert(
                {
                    "flow_id": principal.flow_id,
                    "participant_key": principal.participant_key,
                    "local_date": str(after["local_date"]),
                    "alert_type": alert_type,
                    "tier": str(tier),
                    "observed_value": observed_value,
                    "message": message,
                }
            )

    def flush(self) -> None:
        with self._lock:
            pending = self._pending
            self._pending = {}
            state_snapshots = {
                key: dict(self._states.get(key, {})) for key in pending
            }
        failed: dict[tuple[str, str, str], dict[str, int]] = {}
        for key, deltas in pending.items():
            try:
                policy = self._policy_from_state(state_snapshots[key])
                self.store.increment_daily_traffic_usage(
                    *key,
                    deltas=deltas,
                    traffic_tier=str(policy["traffic_tier"]),
                    refresh_tier=str(policy["refresh_tier"]),
                    effective_factor=float(policy["effective_factor"]),
                )
            except Exception:
                failed[key] = deltas
        if failed:
            with self._lock:
                for key, deltas in failed.items():
                    target = self._pending.setdefault(key, {})
                    for field, amount in deltas.items():
                        target[field] = int(target.get(field, 0)) + int(amount)

    def admin_daily_usage(self, local_date: str | None = None) -> list[dict[str, Any]]:
        self.flush()
        return self.store.list_daily_traffic_usage(local_date or policy_date())


def principal_from_session_record(record: dict[str, Any]) -> RequestPrincipal:
    expires_at = datetime.fromisoformat(str(record["expires_at"]))
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return RequestPrincipal(
        session_id=str(record["id"]),
        token_hash=str(record["token_hash"]),
        principal_type=str(record["principal_type"]),
        flow_id=str(record.get("flow_id") or ""),
        participant_key=str(record.get("participant_key") or ""),
        canonical_participant_name=str(
            record.get("canonical_participant_name") or ""
        ),
        submission_id=str(record.get("submission_id") or ""),
        expires_at_epoch=expires_at.timestamp(),
    )


def tier_index(value: float, thresholds: tuple[float, ...]) -> int:
    index = 0
    for threshold in thresholds:
        if value < threshold:
            break
        index += 1
    return index


def factor_label(value: float) -> str:
    if math.isclose(value, 1.0):
        return "normal speed"
    if math.isclose(value, 0.0):
        return "paused"
    fraction = Fraction(value).limit_denominator(16)
    if math.isclose(value, float(fraction), rel_tol=1e-9, abs_tol=1e-12):
        return f"{fraction.numerator}/{fraction.denominator} normal speed"
    return f"{value:.3g} times normal speed"


def policy_messages(
    traffic_index: int,
    refresh_index: int,
    traffic_factor: float,
    refresh_factor: float,
    effective_factor: float,
    egress_bytes: int,
    reload_count: int,
    settings: ParticipantGovernanceConfig,
) -> list[str]:
    messages: list[str] = []
    if traffic_index:
        threshold_index = min(traffic_index - 1, len(settings.traffic_thresholds_gb) - 1)
        threshold = settings.traffic_thresholds_gb[threshold_index]
        if traffic_factor <= 0:
            messages.append(
                f"Your evaluation traffic reached {threshold:g} GB today. Access is paused until tomorrow."
            )
        else:
            messages.append(
                f"Your traffic reached {threshold:g} GB today. The traffic rate is limited to "
                f"{factor_label(traffic_factor)}."
            )
    if refresh_index:
        threshold_index = min(refresh_index - 1, len(settings.refresh_thresholds) - 1)
        threshold = settings.refresh_thresholds[threshold_index]
        if refresh_factor <= 0:
            messages.append(
                f"You reloaded the evaluation page {reload_count} times today. Access is paused until tomorrow."
            )
        else:
            messages.append(
                f"You reloaded the evaluation page {reload_count} times today. The reload rate is limited to "
                f"{factor_label(refresh_factor)}."
            )
    if (
        traffic_index
        and refresh_index
        and traffic_factor > 0
        and refresh_factor > 0
    ):
        messages.append(
            f"The combined traffic and reload limit is {factor_label(effective_factor)}."
        )
    if effective_factor <= 0:
        messages.append(
            f"Existing input will still be saved. Evaluation can resume after {next_policy_reset()}."
        )
    return messages
