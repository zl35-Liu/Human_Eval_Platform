from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TrafficProtectionConfig:
    enabled: bool = True
    video_requests_per_minute_per_ip: int = 240
    api_requests_per_minute_per_ip: int = 360
    other_requests_per_minute_per_ip: int = 240
    max_video_connections_per_ip: int = 8
    max_video_connections_total: int = 64
    max_http_connections_total: int = 128
    video_bandwidth_mib_per_second_per_ip: float = 8.0
    video_bandwidth_mib_per_second_total: float = 64.0
    socket_timeout_seconds: int = 30


@dataclass(frozen=True)
class ParticipantGovernanceConfig:
    enabled: bool = False
    enforcement_mode: str = "enforce"
    session_ttl_seconds: int = 604_800
    allowlist_reload_seconds: int = 10
    usage_flush_seconds: int = 30
    session_cookie_secure: bool = False
    traffic_thresholds_gb: tuple[float, ...] = (2.0, 5.0, 10.0)
    traffic_factors: tuple[float, ...] = (1.0, 0.75, 0.5, 0.0)
    refresh_thresholds: tuple[int, ...] = (25, 50, 100)
    refresh_factors: tuple[float, ...] = (1.0, 0.75, 0.5, 0.0)


@dataclass(frozen=True)
class AppConfig:
    host: str
    port: int
    database_path: Path
    flow_dir: Path
    video_dir: Path
    export_dir: Path
    static_dir: Path
    seed_flow_path: Path
    extra_video_dirs: tuple[Path, ...] = ()
    video_preview_dir: Path | None = None
    video_cache_max_age_seconds: int = 31_536_000
    traffic_protection: TrafficProtectionConfig = field(default_factory=TrafficProtectionConfig)
    participant_allowlist_path: Path | None = None
    participant_governance: ParticipantGovernanceConfig = field(
        default_factory=ParticipantGovernanceConfig
    )


DEFAULT_CONFIG: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8000,
    "database_path": "storage/human_eval.db",
    "flow_dir": "data/flows",
    "video_dir": "storage/videos",
    "extra_video_dirs": [],
    "video_preview_dir": "storage/video_previews",
    "export_dir": "storage/exports",
    "static_dir": "static",
    "seed_flow_path": "data/flows/default_flow.json",
    "video_cache_max_age_seconds": 31_536_000,
    "traffic_protection": {},
    "participant_allowlist_path": "docs/participant-allowlist.md",
    "participant_governance": {},
}


def _read_config_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Configuration file must contain a JSON object: {path}")
    return data


def _resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _resolve_path_list(value: Any, base_dir: Path) -> tuple[Path, ...]:
    if value in (None, ""):
        return ()
    values = [value] if isinstance(value, (str, os.PathLike)) else value
    if not isinstance(values, list):
        raise ValueError("extra_video_dirs must be a string or list")
    return tuple(_resolve_path(str(item), base_dir) for item in values if str(item).strip())


def _load_traffic_protection(value: Any) -> TrafficProtectionConfig:
    if value in (None, ""):
        values = {}
    elif isinstance(value, dict):
        values = value
    else:
        raise ValueError("traffic_protection must be a JSON object")

    defaults = TrafficProtectionConfig()
    enabled = values.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        raise ValueError("traffic_protection.enabled must be true or false")

    integer_fields = (
        "video_requests_per_minute_per_ip",
        "api_requests_per_minute_per_ip",
        "other_requests_per_minute_per_ip",
        "max_video_connections_per_ip",
        "max_video_connections_total",
        "max_http_connections_total",
        "socket_timeout_seconds",
    )
    parsed: dict[str, Any] = {"enabled": enabled}
    for name in integer_fields:
        raw = values.get(name, getattr(defaults, name))
        if isinstance(raw, bool):
            raise ValueError(f"traffic_protection.{name} must be a positive integer")
        try:
            parsed[name] = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"traffic_protection.{name} must be a positive integer") from exc
        if parsed[name] <= 0:
            raise ValueError(f"traffic_protection.{name} must be a positive integer")

    bandwidth_fields = (
        "video_bandwidth_mib_per_second_per_ip",
        "video_bandwidth_mib_per_second_total",
    )
    for name in bandwidth_fields:
        raw = values.get(name, getattr(defaults, name))
        if isinstance(raw, bool):
            raise ValueError(f"traffic_protection.{name} must be positive")
        try:
            parsed[name] = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"traffic_protection.{name} must be positive") from exc
        if parsed[name] <= 0:
            raise ValueError(f"traffic_protection.{name} must be positive")

    return TrafficProtectionConfig(**parsed)


def _number_tuple(
    values: dict[str, Any],
    name: str,
    default: tuple[float, ...],
    *,
    integers: bool = False,
) -> tuple[Any, ...]:
    raw = values.get(name, default)
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"participant_governance.{name} must be an array")
    parsed: list[Any] = []
    for item in raw:
        if isinstance(item, bool):
            raise ValueError(f"participant_governance.{name} contains an invalid number")
        try:
            parsed.append(int(item) if integers else float(item))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"participant_governance.{name} contains an invalid number"
            ) from exc
    return tuple(parsed)


def _load_participant_governance(value: Any) -> ParticipantGovernanceConfig:
    if value in (None, ""):
        values = {}
    elif isinstance(value, dict):
        values = value
    else:
        raise ValueError("participant_governance must be a JSON object")

    defaults = ParticipantGovernanceConfig()
    enabled = values.get("enabled", defaults.enabled)
    secure = values.get("session_cookie_secure", defaults.session_cookie_secure)
    if not isinstance(enabled, bool) or not isinstance(secure, bool):
        raise ValueError(
            "participant_governance.enabled and session_cookie_secure must be boolean"
        )
    mode = str(values.get("enforcement_mode", defaults.enforcement_mode)).strip().lower()
    if mode not in {"observe", "enforce"}:
        raise ValueError(
            "participant_governance.enforcement_mode must be observe or enforce"
        )

    parsed_integers: dict[str, int] = {}
    for name in ("session_ttl_seconds", "allowlist_reload_seconds", "usage_flush_seconds"):
        raw = values.get(name, getattr(defaults, name))
        if isinstance(raw, bool):
            raise ValueError(f"participant_governance.{name} must be a positive integer")
        try:
            parsed_integers[name] = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"participant_governance.{name} must be a positive integer"
            ) from exc
        if parsed_integers[name] <= 0:
            raise ValueError(f"participant_governance.{name} must be a positive integer")

    traffic_thresholds = _number_tuple(
        values,
        "traffic_thresholds_gb",
        defaults.traffic_thresholds_gb,
    )
    traffic_factors = _number_tuple(
        values,
        "traffic_factors",
        defaults.traffic_factors,
    )
    refresh_thresholds = _number_tuple(
        values,
        "refresh_thresholds",
        defaults.refresh_thresholds,
        integers=True,
    )
    refresh_factors = _number_tuple(
        values,
        "refresh_factors",
        defaults.refresh_factors,
    )
    if (
        not traffic_thresholds
        or not refresh_thresholds
        or len(traffic_factors) != len(traffic_thresholds) + 1
        or len(refresh_factors) != len(refresh_thresholds) + 1
    ):
        raise ValueError("participant_governance threshold and factor counts do not match")
    if (
        tuple(sorted(traffic_thresholds)) != traffic_thresholds
        or tuple(sorted(refresh_thresholds)) != refresh_thresholds
        or traffic_thresholds[0] <= 0
        or refresh_thresholds[0] <= 0
    ):
        raise ValueError("participant_governance thresholds must be positive and ascending")
    if any(factor < 0 or factor > 1 for factor in (*traffic_factors, *refresh_factors)):
        raise ValueError("participant_governance factors must be between 0 and 1")

    return ParticipantGovernanceConfig(
        enabled=enabled,
        enforcement_mode=mode,
        session_cookie_secure=secure,
        traffic_thresholds_gb=traffic_thresholds,
        traffic_factors=traffic_factors,
        refresh_thresholds=refresh_thresholds,
        refresh_factors=refresh_factors,
        **parsed_integers,
    )


def load_config(config_path: str | None = None) -> AppConfig:
    root_dir = Path.cwd().resolve()
    selected_path = config_path or os.environ.get("HEP_CONFIG")
    file_values: dict[str, Any] = {}
    base_dir = root_dir

    if selected_path:
        path = Path(selected_path).expanduser().resolve()
        file_values = _read_config_file(path)
        base_dir = path.parent
    elif Path("config.json").exists():
        path = Path("config.json").resolve()
        file_values = _read_config_file(path)
        base_dir = path.parent

    merged = {**DEFAULT_CONFIG, **file_values}
    host = str(os.environ.get("HEP_HOST", merged["host"]))
    port = int(os.environ.get("HEP_PORT", merged["port"]))

    return AppConfig(
        host=host,
        port=port,
        database_path=_resolve_path(str(merged["database_path"]), base_dir),
        flow_dir=_resolve_path(str(merged["flow_dir"]), base_dir),
        video_dir=_resolve_path(str(merged["video_dir"]), base_dir),
        export_dir=_resolve_path(str(merged["export_dir"]), base_dir),
        static_dir=_resolve_path(str(merged["static_dir"]), base_dir),
        seed_flow_path=_resolve_path(str(merged["seed_flow_path"]), base_dir),
        extra_video_dirs=_resolve_path_list(merged.get("extra_video_dirs"), base_dir),
        video_preview_dir=_resolve_path(str(merged["video_preview_dir"]), base_dir),
        video_cache_max_age_seconds=max(0, int(merged["video_cache_max_age_seconds"])),
        traffic_protection=_load_traffic_protection(merged.get("traffic_protection")),
        participant_allowlist_path=_resolve_path(
            str(merged["participant_allowlist_path"]),
            base_dir,
        ),
        participant_governance=_load_participant_governance(
            merged.get("participant_governance")
        ),
    )
