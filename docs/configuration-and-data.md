# Configuration and Data

## Runtime Configuration

Configuration precedence is: `--config`, `HEP_CONFIG`, `config.json`, then built-in defaults.

- `HEP_HOST`: override the listening address.
- `HEP_PORT`: override the listening port.
- `HEP_ADMIN_PASSWORD`: enable administration and results access.
- `HEP_IMPORT_OVERWRITE=1`: replace an existing workflow with matching JSON.
- `HEP_DEBUG=1`: enable HTTP request logging.

Keep real credentials, public addresses, and absolute deployment paths outside Git. Use an ignored `config.json` for deployment-specific settings.

## Runtime Data

The default SQLite database is `storage/human_eval.db`. Its main tables are:

- `flows`: imported and published workflows.
- `submissions`: participant fields, answers, video order, and review state.
- `participant_sessions`: hashed session tokens.
- `daily_traffic_usage`: per-participant daily request and transfer totals.
- `traffic_alerts`: traffic and reload threshold events.

Each answer key follows `<video-id>:<dimension-id>:<question-id>`. Answers contain a score, confidence, explanation, and optional text or video-time references.

Use SQLite online backup or stop the service before copying a database. Copying only an active WAL database file may produce an incomplete backup.

## Video Delivery

Videos live under `storage/videos/` or configured `extra_video_dirs`. The `/videos/` endpoint supports browser caching, conditional requests, and byte ranges.

Timeline previews require `ffmpeg` and `ffprobe`:

```bash
python3 scripts/generate_video_previews.py --config config.example.json
```
