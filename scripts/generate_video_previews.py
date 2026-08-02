#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from human_eval_platform.config import AppConfig, load_config
from human_eval_platform.server import find_video_file, normalize_video_preview_path, video_preview_id


DEFAULT_INTERVAL_SECONDS = 1
DEFAULT_THUMB_WIDTH = 320
DEFAULT_THUMB_HEIGHT = 180
DEFAULT_COLUMNS = 10
DEFAULT_ROWS = 10


def flow_media_path(path: Any, flow: dict[str, Any]) -> str:
    value = normalize_video_preview_path(str(path or "").strip())
    if not value or value.startswith(("http://", "https://")):
        return value
    folder = normalize_video_preview_path(str(flow.get("videoFolder", "")).strip())
    if "/" in value or not folder:
        return value
    return f"{folder}/{value}"


def flow_video_paths(flow: dict[str, Any], include_example: bool = True) -> list[str]:
    paths = [
        flow_media_path(video.get("fileName"), flow)
        for video in flow.get("videos", [])
        if isinstance(video, dict)
    ]
    if include_example:
        paths.append(flow_media_path(flow.get("instructions", {}).get("exampleVideoPath"), flow))
    return list(dict.fromkeys(path for path in paths if path and not path.startswith(("http://", "https://"))))


def executable_path(value: str | None, fallback_name: str) -> Path:
    candidate = Path(value).expanduser().resolve() if value else None
    if candidate is None:
        located = shutil.which(fallback_name)
        candidate = Path(located).resolve() if located else None
    if candidate is None or not candidate.is_file():
        raise RuntimeError(f"{fallback_name} executable not found")
    return candidate


def probe_duration(ffprobe: Path, source: Path) -> float:
    command = [
        str(ffprobe),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(source),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    duration = float(result.stdout.strip())
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid video duration: {source}")
    return duration


def manifest_is_current(
    manifest_path: Path,
    source: Path,
    video_path: str,
    interval: int,
    width: int,
    height: int,
    columns: int,
    rows: int,
) -> bool:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    stat = source.stat()
    expected = {
        "videoPath": normalize_video_preview_path(video_path),
        "sourceSize": stat.st_size,
        "sourceMtimeNs": stat.st_mtime_ns,
        "intervalSeconds": interval,
        "thumbWidth": width,
        "thumbHeight": height,
        "columns": columns,
        "rows": rows,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        return False
    sheets = manifest.get("sheets")
    return bool(
        isinstance(sheets, list)
        and sheets
        and all(isinstance(name, str) and (manifest_path.parent / name).is_file() for name in sheets)
    )


def install_generated_preview(temp_dir: Path, target_dir: Path) -> None:
    backup_dir: Path | None = None
    try:
        if target_dir.exists():
            backup_dir = target_dir.with_name(f".{target_dir.name}.old-{uuid.uuid4().hex}")
            target_dir.rename(backup_dir)
        temp_dir.rename(target_dir)
    except Exception:
        if backup_dir is not None and backup_dir.exists() and not target_dir.exists():
            backup_dir.rename(target_dir)
        raise
    if backup_dir is not None:
        shutil.rmtree(backup_dir)


def generate_preview(
    config: AppConfig,
    ffmpeg: Path,
    ffprobe: Path,
    video_path: str,
    interval: int,
    width: int,
    height: int,
    columns: int,
    rows: int,
    threads: int,
    force: bool,
) -> str:
    if config.video_preview_dir is None:
        raise RuntimeError("video_preview_dir is not configured")
    source = find_video_file(config, video_path)
    if source is None:
        return "missing"

    preview_root = config.video_preview_dir.resolve()
    preview_root.mkdir(parents=True, exist_ok=True)
    target_dir = preview_root / video_preview_id(video_path)
    manifest_path = target_dir / "manifest.json"
    if not force and manifest_is_current(
        manifest_path,
        source,
        video_path,
        interval,
        width,
        height,
        columns,
        rows,
    ):
        return "current"

    duration = probe_duration(ffprobe, source)
    frame_count = max(1, math.ceil(duration / interval))
    frames_per_sheet = columns * rows
    expected_sheet_count = math.ceil(frame_count / frames_per_sheet)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".preview-{target_dir.name[:12]}-", dir=str(preview_root)))
    try:
        filter_graph = (
            f"fps=1/{interval},"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
            f"tile={columns}x{rows}:nb_frames={frames_per_sheet}:padding=0:margin=0"
        )
        command = [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            filter_graph,
            "-vsync",
            "vfr",
            "-threads",
            str(max(1, threads)),
            "-q:v",
            "5",
            "-start_number",
            "0",
            str(temp_dir / "sheet-%04d.jpg"),
        ]
        subprocess.run(command, check=True)
        sheets = sorted(path.name for path in temp_dir.glob("sheet-*.jpg"))
        if len(sheets) != expected_sheet_count:
            raise RuntimeError(
                f"Unexpected preview sheet count for {video_path}: "
                f"expected {expected_sheet_count}, found {len(sheets)}"
            )

        stat = source.stat()
        manifest = {
            "version": 1,
            "videoPath": normalize_video_preview_path(video_path),
            "sourceSize": stat.st_size,
            "sourceMtimeNs": stat.st_mtime_ns,
            "duration": duration,
            "intervalSeconds": interval,
            "frameCount": frame_count,
            "thumbWidth": width,
            "thumbHeight": height,
            "columns": columns,
            "rows": rows,
            "framesPerSheet": frames_per_sheet,
            "sheets": sheets,
        }
        (temp_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        install_generated_preview(temp_dir, target_dir)
        return "generated"
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate timeline preview sheets for evaluation videos.")
    parser.add_argument("--config", default=None, help="Path to the application JSON configuration.")
    parser.add_argument("--flow-file", default=None, help="Workflow JSON file; defaults to seed_flow_path.")
    parser.add_argument("--ffmpeg", default=None, help="Path to the ffmpeg executable.")
    parser.add_argument("--ffprobe", default=None, help="Path to the ffprobe executable.")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--width", type=int, default=DEFAULT_THUMB_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_THUMB_HEIGHT)
    parser.add_argument("--columns", type=int, default=DEFAULT_COLUMNS)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--exclude-example", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    for name in ("interval", "width", "height", "columns", "rows", "threads"):
        if getattr(args, name) <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")

    config = load_config(args.config)
    flow_path = Path(args.flow_file).expanduser().resolve() if args.flow_file else config.seed_flow_path
    flow = json.loads(flow_path.read_text(encoding="utf-8"))
    if not isinstance(flow, dict):
        raise SystemExit("Workflow file must contain a JSON object")

    ffmpeg = executable_path(args.ffmpeg, "ffmpeg")
    default_ffprobe = str(ffmpeg.with_name("ffprobe")) if ffmpeg.with_name("ffprobe").is_file() else None
    ffprobe = executable_path(args.ffprobe or default_ffprobe, "ffprobe")
    paths = flow_video_paths(flow, include_example=not args.exclude_example)
    counts = {"generated": 0, "current": 0, "missing": 0}
    print(
        f"Generating previews for {len(paths)} videos at {args.interval}-second intervals; "
        f"output directory: {config.video_preview_dir}",
        flush=True,
    )
    for index, video_path in enumerate(paths, start=1):
        status = generate_preview(
            config=config,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            video_path=video_path,
            interval=args.interval,
            width=args.width,
            height=args.height,
            columns=args.columns,
            rows=args.rows,
            threads=args.threads,
            force=args.force,
        )
        counts[status] += 1
        status_text = {"generated": "generated", "current": "current", "missing": "missing"}[status]
        print(f"[{index}/{len(paths)}] {status_text}: {video_path}", flush=True)
    print(json.dumps(counts, ensure_ascii=False), flush=True)
    if counts["missing"]:
        raise SystemExit(f"{counts['missing']} video files were not found")


if __name__ == "__main__":
    main()
