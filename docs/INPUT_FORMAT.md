# Input contracts

Each run processes one show's episodes. Episode numbers are nonnegative integers, need not start at one, and may contain gaps. Their values are source labels, not ranking features. Speaker labels, character names and story content are unrestricted text.

## Timed screenplay TXT

Use this layout, illustrated by the original synthetic file in `examples/scifi/airlock.txt`:

```text
EPISODE NUMBER: Episode 204
EPISODE LOGLINE: Optional editor context.

--- SCRIPT START ---

SCENE 01
Scene heading: INT. CONTROL ROOM

**SPEAKER (Urgent):**
Start: (00:00.00) | End: (00:02.50)
"The airlock opened from inside!"

--- SCRIPT END ---
```

`EPISODE NUMBER` and complete timed blocks are required. A speaker block is dialogue; `ACTION`, `CAMERA` and `SOUND FX` identify the other supported tracks. Dialogue and camera/action blocks may overlap. Formats are MM:SS.fraction or HH:MM:SS.fraction, with up to three fractional digits. These are elapsed times, not frame counts. Line endings and a UTF-8 BOM are normalized; text is otherwise preserved. The parser rejects malformed or missing beat timings instead of silently skipping them.

## Normalized JSON

Convert other timed formats to:

```json
{
  "episodes": [{
    "episode": 907,
    "logline": "Optional context for discovery, withheld from review.",
    "beats": [{
      "kind": "DIALOGUE",
      "label": "CAPTAIN",
      "start_ms": 600000,
      "end_ms": 602500,
      "text": "The scoreboard changed before she scored!"
    }]
  }]
}
```

`start_ms` and `end_ms` are integer milliseconds with `0 <= start_ms < end_ms`. All five beat fields are required; kinds are `DIALOGUE`, `ACTION`, `CAMERA`, `SOUND FX`. `logline` is optional. The sample above demonstrates the schema; a single short beat does not meet the default minimum promo duration. A complete synthetic episode is in `examples/sports/scoreboard.json`.

Input adapters must preserve timing and wording, not add imagined events or approximate timestamps. The normalizer assigns beat IDs for references. Output retains filename, episode, source fingerprint and TXT line or JSON beat ID, so every cut is traceable.

## Web uploads

The Streamlit workspace accepts multiple TXT/JSON files or ZIP archives containing them, including nested directories. UTF-8 text is required. Metadata files (`.DS_Store`, `__MACOSX`) and unsupported extensions inside archives are skipped and counted. A malformed supported script rejects the upload atomically; no partial pack is silently analyzed.

Web limits: 25 MB total compressed upload, 50 MB expanded, 200 script files, 200 episodes, 20,000 beats total and 1,000 beats per episode. Archives with unsafe paths, symlinks, encryption, excessive compression or too many entries are rejected. No archive is extracted to the filesystem. Very large episodes may also exceed the bounded model prompt size; split them into timestamp-preserving parts with distinct episode identifiers. These resource limits are product constraints, independent of the benchmark pack.
