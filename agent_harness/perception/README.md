# Harness perception

The backend accepts structured data and metadata. These MCP tools run beside
it: FFmpeg supplies images and audio samples, the host model reads images,
and an optional offline Whisper recognizer transcribes speech. ASR output is
an estimate to review, not a verified statement about the task.

## Prepare and run

Copy an existing CTranslate2 Whisper model into a new directory. The source
must contain `model.bin`, `config.json`, `tokenizer.json` and `vocabulary.txt`:

```sh
uv run python -m agent_harness.perception.prepare_asr \
  --source-model /path/to/asr_models/medium --out .cache/asr/local/medium
docker build -f agent_harness/hosts/opencode.Dockerfile -t intentum-opencode:audio .
uv run python -m agent_harness.scenarios.dataspace.agent \
  --task task_312 --runs 1 --video --asr-model .cache/asr/local/medium \
  --image intentum-opencode:audio --out /tmp/intentum-audio-run-new
```

The copy command verifies every file against its source SHA256 and records
the source directory. It leaves the source untouched and refuses an existing
destination. Both the model cache and run artifacts are gitignored. The
weights are mounted read-only at `/opt/asr-model`; they are not copied into
the image or backend wheel. Startup verifies the copied files again.

If local weights are unavailable, the separate preparation command can
download a pinned `Systran/faster-whisper-medium` revision:

```sh
uv run --group perception python -m agent_harness.perception.prepare_asr \
  --out .cache/asr/downloaded/medium
```

This explicit setup step is the only downloader. Runtime recognition uses
the supplied directory with `local_files_only=True` and offline environment
settings. `--asr-model` requires `--video` and the OpenCode host; omit it for
frame-only tools. The chat model needs image input for frames, but no audio
input capability: it receives transcript text from MCP. The image installs
the separately locked `perception` dependency group; backend dependencies
stay unchanged.

## Tools and evidence

| Tool | Output |
|---|---|
| `inspect_video` | Video duration, video stream and audio stream metadata. `audio_stream` is the ordinal in the audio stream list, not the file-wide stream index. |
| `read_video_frames` | Up to six PNG attachments, requested/actual timestamps and source/frame hashes. |
| `transcribe_audio` | Speech from one track and interval, with estimated segment times on the video's zero-origin clock and importable segment JSON. Default interval: first 90 s; maximum per call: 180 s. |
| `record_observation` | An agent statement citing `frame_ids`, `segment_ids`, or both. For speech alone use `frame_ids=[]`. A correction names `supersedes` and `reason`, retaining earlier observations and raw ASR. |

Audio extraction produces mono 16 kHz PCM, retains the selected track's
offset relative to the first video timestamp, and fills timeline gaps with
silence before cutting the requested interval. ASR runs in a subprocess on
CPU/int8 with four threads, beam size 5, temperature 0, voice activity
detection and word timestamps. It does not use the question, a task-derived
initial prompt, hotwords, translation or spelling normalization. Language
is detected from audio unless the caller supplies a Whisper language code.
The [recognizer options](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/faster_whisper/transcribe.py)
come from faster-whisper 1.2.1.

The worker is terminated after 300 s. Clips, paths, track ordinals and
returned segment times are validated. Requests are serialized in one server
to bound model memory; successful identical requests reuse their stored
transcript. Source/model identity, recognition options and reader code
hashes are part of that request identity. The raw result also records runtime
library versions. JSON records are published atomically.

Under each run's `perception/`:

- `frames/`: PNG images and frame metadata.
- `audio/`: extracted WAV, raw ASR with word times relative to the clip,
  importable `*.segments.json` with video-clock times, and completed requests.
- `segments/`: individual records addressable by `segment_id`.
- `observations/`: agent interpretations and explicit revisions.

Import `segments_path` through `backend_import_dataset` before using speech
as data. The recognizer never imports into the backend itself. If it detects
no speech, `segments_path` is null and no empty table is fabricated. The tool
explains this gap; absence of recognized speech is not proof of silence.
Check important numbers, comparisons and terms against frames or another
reading. These tools do not enforce that the agent performs the review, and
they do not resolve disagreements between speech and the display.
