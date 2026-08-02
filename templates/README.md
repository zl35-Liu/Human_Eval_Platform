# Workflow Template

Copy `evaluation_flow_template.json` into `data/flows/`, then update the workflow identifier, media, dimensions, and scoring rules.

```bash
cp templates/evaluation_flow_template.json data/flows/my-evaluation.json
mkdir -p storage/videos/my-evaluation
```

Key fields:

- `id`: unique workflow identifier.
- `title`: title displayed in the interface.
- `videoFolder`: subdirectory under `storage/videos/`.
- `participantFields`: fields collected during sign-in.
- `instructions`: overview, scoring guide, and optional example media.
- `videos`: video identifiers, filenames, and optional transcript filenames.
- `dimensions[].questions`: dimensions, criteria, descriptions, and scoring rubrics.
- `responseConfig`: score range, confidence range, and explanation requirements.

Place transcripts beside their videos. Set `textFileName` explicitly when possible; otherwise the server attempts a same-directory filename match.

Workflow JSON files are imported at startup. Existing workflow identifiers are not overwritten unless `HEP_IMPORT_OVERWRITE=1` is set for that run.
