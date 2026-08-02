# Architecture

The platform uses a lightweight stack: a Python standard-library HTTP server, SQLite, and a vanilla HTML/CSS/JavaScript frontend.

## Modules

- `app.py`: application entry point.
- `human_eval_platform/config.py`: configuration loading and path resolution.
- `human_eval_platform/store.py`: schema management, validation, autosave, review, and CSV export.
- `human_eval_platform/governance.py`: allowlist, sessions, traffic accounting, and usage policies.
- `human_eval_platform/server.py`: HTTP API, static assets, video caching, and Range responses.
- `static/`: single-page user interface.
- `data/flows/`: workflow definitions imported at startup.
- `templates/`: reusable workflow template.
- `scripts/generate_video_previews.py`: optional offline timeline-preview generator.

## Data Flow

1. The server imports workflow JSON into SQLite at startup.
2. An allowlisted participant receives a session token stored only as a hash.
3. The first login creates and persists a participant-specific video order.
4. The frontend autosaves changed answers without overwriting unrelated answers.
5. Administrators review responses, request revisions, inspect statistics, and export CSV.
6. Video files are streamed in fixed chunks with ETag and Range support.
