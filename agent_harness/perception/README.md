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
| `inspect_video` | Video duration, video/audio streams and container timing metadata (`format`). `audio_stream` is the ordinal in the audio stream list, not the file-wide stream index. |
| `read_video_frames` | Up to six PNG attachments, requested/actual timestamps and source/frame hashes. |
| `transcribe_audio` | Speech from one track and interval, with estimated segment times on the video's zero-origin clock, track bounds and importable segment JSON. Default interval: first 90 s; maximum per call: 180 s. |
| `record_observation` | An agent statement citing `frame_ids`, `segment_ids`, or both. For speech alone use `frame_ids=[]`. A correction names `supersedes` and `reason`, retaining earlier observations and raw ASR. |

Frame decoding seeks near each requested position and selects the first
frame at or after it using original PTS. Explicit absolute seeking and
disabled automatic seek trimming preserve timestamps when the container
and video start at different times. Each decode retains its 12 s budget;
pathological GOPs or damaged media can still leave an unread interval.
A timeout reports that coverage gap, with no claim about the content.

Audio extraction produces mono 16 kHz PCM, retains the selected track's
offset relative to the first video timestamp, and fills gaps inside the
track with silence before cutting the requested interval. A track's endpoint
is its start plus duration, minus the video start. Audio can extend beyond the
video endpoint. `audio_bounds` returns its start/end on this shared clock
and the endpoint's source. When stream duration is unavailable or invalid,
container start plus duration supplies an estimated endpoint
(`end_source="format.duration"`, `end_is_estimate=true`): that container
value may include offsets or other longer tracks. A track declaring a start at
or after that estimate has an unknown extent and is refused. Neither edge of
the cut is padded. A request overlapping the track is cut back to it: it reads
from the track's first samples, so `clip_start_s` can be later than the
requested `start_s`, and stops at audio EOF without trailing silence.
`clip_start_s` is where the cut began; `clip_end_s` and `decoded_duration_s`
are measured from the decoded result. `duration_s` sizes the requested window,
not the clip, so reading a late track returns less than `duration_s`. An
interval wholly outside the track is refused as unread; it is not reported as
silence. Range refusals identify the selected track, the boundary being used
and the readable interval.

ASR runs in a subprocess on
CPU/int8 with four threads, beam size 5, temperature 0, voice activity
detection and word timestamps. It does not use the question, a task-derived
initial prompt, hotwords, translation or spelling normalization. Language
is detected from audio unless the caller supplies a Whisper language code.
The [recognizer options](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/faster_whisper/transcribe.py)
come from faster-whisper 1.2.1.

The worker is terminated after 300 s. Clips, paths, track ordinals and
returned segment times are validated. Requests are serialized in one server
to bound model memory; successful identical requests reuse their stored
transcript. Source/model identity, recognition options, track bounds, video
clock origin, and code hashes for `audio.py`, `video.py` and `asr.py` are part
of that request identity. The raw result also records runtime library
versions. JSON records are published atomically. FFmpeg, FFprobe and ASR
subprocesses all receive a closed stdin so they cannot consume MCP requests.

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

## Media review validation (2026-09-10)

The regressions in `tests/test_perception.py` and `tests/test_audio.py` use
synthetic media, independent of benchmark task content. Real FFmpeg checks
cover variable frame rate and long GOPs with distinct video/container
origins, a 5 s video with 20 s audio, a second delayed shorter track, and
missing stream durations. Seeked frames match full decoding in both PTS and
PNG hash; audio requests beginning at 6 s and 12 s reach beyond the video.
The cache and subprocess tests also check shared-reader changes, timing
changes, track-specific diagnostics and stdin isolation.

All 19 media tests passed inside the FFmpeg 5.1.9 audio image. The host suite
passed 263 tests; its six real-media tests were skipped locally because
FFmpeg is supplied by the image. No task score or ASR accuracy measurement
was repeated for these deterministic reader fixes.

A separate network-disabled container probe used a 994.07 s, 502,198,948-byte
1080p30 `testsrc2` MP4, made by remuxing a 2 s closed GOP loop encoded with
libx264 medium/CRF 26. A six-frame request spanning 872.83–993.69 s took
1.08 s overall, with each decode taking 0.13–0.16 s. Full decoding of the
last requested frame took 16.85 s (the measurement allowed 60 s); its PTS
and PNG hash matched the seeked result. This demonstrates removal of the
decode-from-zero cost on that fixture, not a latency guarantee for all
codecs or GOP lengths. That probe ran outside the repository and its code and
timings are not carried here; the fixture description above is what reproduces
it.

## Leading-gap follow-up (2026-09-10)

Reviewing the fixes above found the head of the interval still padded. A
request beginning before the selected track returned synthesized silence as
decoded audio, and a request lying wholly before the track returned nothing but
silence under a success status: the failure the endpoint fix removed, surviving
at the other edge. Reading now starts at the track's first samples and a wholly
earlier interval is refused, so neither edge is padded. The two edges are
established differently, and only the policy is symmetric: the endpoint is
measured from the decoded WAV, while the start comes from the track's declared
`start_time`. A container that understates that start can still leave
synthesized silence at the head. A container that overstates it can trim real
samples at the head, and may refuse a readable interval as entirely before the
track. The regressions in `tests/test_audio.py` cover
the clamped start, the requested-window policy, the refusals, and the absence of
leading padding in real decoded audio.
