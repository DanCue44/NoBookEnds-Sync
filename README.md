# NoBookEnds Sync

Self-hosted companion server for the NoBookEnds app. It sits next to
Audiobookshelf (ABS) and does two things:

* **Listen & Read:** aligns an audiobook's narration to its EPUB so the app
  can highlight the sentence and word being read. It transcribes the audio
  once with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (word
  timestamps, CTranslate2, GPU or CPU), aligns the transcript to the book text
  and serves the result to any ABS user who can access both items.
* **User data:** keeps what Audiobookshelf has no place for: podcast
  subscriptions made in the app and their progress, and ratings,
  highlights/notes and clips synced across one reader's devices
  ([below](#user-data)).

NoBookEnds works without it, but the Sync server is **recommended for the
full NoBookEnds experience**: word-level Listen & Read (without it the app
falls back to an approximate on-phone match), podcast subscriptions in the
app, and ratings, highlights and clips that follow a reader across devices.
The CPU image is enough for everything except fast alignment: user data never
needs a GPU, and on a CPU alignment simply runs slower (overnight by default).

## Install

### Easiest: install it from Allseerr

If you run [Allseerr](https://github.com/DanCue44/Allseerr) with Audiobookshelf
connected under Settings > User Access (with an Audiobookshelf admin API key),
open **Settings > NoBookEnds Sync** there. Allseerr:

1. makes a read only Audiobookshelf account for the service (`nobookends-sync`,
   download only, all libraries) and its API key, and an admin token, so you
   never copy a key by hand;
2. installs the container for you when its Docker socket is mounted
   (`/var/run/docker.sock`; it detects an NVIDIA runtime and picks the GPU
   image, or lets you pick the CPU image), on the same Docker network as
   Allseerr, with a restart policy and the data folder you choose (Unraid
   default `/mnt/user/appdata/nobookends-sync`). Without the socket it writes a
   Docker Compose file, a `docker run` command and an Unraid template with
   every value filled in, and then tests the connection;
3. serves the reader API to phones under its own address
   (`https://<allseerr>/api/v1/nbe-sync`), so the service needs **no public
   address and no reverse proxy**. Allseerr streams each request on with the
   reader's own Audiobookshelf token (the service still checks it with
   Audiobookshelf) and never serves the admin API. The NoBookEnds app picks
   that address up from Allseerr by itself.

Allseerr runs as a normal user, so besides the path it needs the group that
owns the socket (Unraid: Extra Parameters `--group-add 281`, or whatever group
id `ls -ln /var/run/docker.sock` shows; the settings page names it).
Mounting the Docker socket gives Allseerr control of Docker on that server
(it could start, stop or create any container). Allseerr only uses it when you
press Install, and you can remove the mount afterwards; the service keeps
running. The rest of this section is the manual install.

### By hand

You need:

1. Audiobookshelf with your audiobooks and their EPUBs (same item, or
   separate libraries; the service pairs them by ISBN/ASIN or title + author).
2. An Audiobookshelf user and API key for the service
   ([below](#audiobookshelf-user-and-api-key)).
3. A public HTTPS address for the service, so phones can reach it away from
   home ([reverse proxy](#reverse-proxy-and-public-url)), unless Allseerr
   serves it ([above](#easiest-install-it-from-allseerr)).
4. Optional but much faster: an NVIDIA GPU with the NVIDIA Container Toolkit
   (Unraid: the Nvidia Driver plugin).

### Images

| Image | For |
| --- | --- |
| `ghcr.io/dancue44/nobookends-sync:latest` (same as `:cuda`) | NVIDIA GPU (CUDA 12, works back to Pascal cards such as the Quadro P2000), falls back to CPU |
| `ghcr.io/dancue44/nobookends-sync:cpu` | any x86-64 server, no GPU libraries, about 600 MB instead of about 5 GB |

Releases are also tagged `X.Y.Z` / `X.Y.Z-cuda` and `X.Y.Z-cpu`.

### Docker Compose (NVIDIA GPU)

```yaml
services:
  nobookends-sync:
    image: ghcr.io/dancue44/nobookends-sync:latest
    container_name: nobookends-sync
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      ABS_URL: http://audiobookshelf:80      # ABS as seen from this container
      ABS_API_KEY: ${ABS_API_KEY}            # put secrets in a .env file next to compose.yaml
      ADMIN_TOKEN: ${ADMIN_TOKEN}            # optional, enables the admin API (AllSeerr)
      TZ: America/Chicago                    # the auto window uses this time zone
      AUTO_WINDOW: "01:00-06:00"
    volumes:
      - ./nobookends-sync:/data
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

### Docker Compose (CPU only)

```yaml
services:
  nobookends-sync:
    image: ghcr.io/dancue44/nobookends-sync:cpu
    container_name: nobookends-sync
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      ABS_URL: http://audiobookshelf:80
      ABS_API_KEY: ${ABS_API_KEY}
      ADMIN_TOKEN: ${ADMIN_TOKEN}
      TZ: America/Chicago
      AUTO_WINDOW: "01:00-06:00"
      # WHISPER_CPU_THREADS: "auto"          # default: half the CPUs, at most 16
      # WHISPER_MODEL: base.en               # about 2x faster than small.en on CPU
    volumes:
      - ./nobookends-sync:/data
    # cpus: 6                                # optional hard limit; threads follow it
```

If Audiobookshelf runs in the same Compose project, `http://audiobookshelf:80`
reaches it by service name. Otherwise use the host and port, for example
`http://192.168.1.10:13378`.

### docker run

```bash
# GPU (NVIDIA Container Toolkit)
docker run -d --name nobookends-sync --restart unless-stopped \
  --gpus all \
  -p 8000:8000 \
  -e ABS_URL=http://192.168.1.10:13378 \
  -e ABS_API_KEY=... -e ADMIN_TOKEN=... \
  -e TZ=America/Chicago -e AUTO_WINDOW=01:00-06:00 \
  -v /srv/nobookends-sync:/data \
  ghcr.io/dancue44/nobookends-sync:latest

# CPU: drop --gpus all and use the :cpu tag
docker run -d --name nobookends-sync --restart unless-stopped \
  -p 8000:8000 \
  -e ABS_URL=http://192.168.1.10:13378 \
  -e ABS_API_KEY=... -e ADMIN_TOKEN=... \
  -e TZ=America/Chicago -e AUTO_WINDOW=01:00-06:00 \
  -v /srv/nobookends-sync:/data \
  ghcr.io/dancue44/nobookends-sync:cpu
```

Check it: `curl http://localhost:8000/api/v1/health` returns
`{"ok":true,"version":"...","gpu":true}` (`gpu` is false on the CPU image or
when no GPU is visible).

### Unraid

Two templates are in [`templates/`](templates/):

* [`nobookends-sync.xml`](templates/nobookends-sync.xml): GPU image, needs the
  **Nvidia Driver** plugin (adds `--runtime=nvidia` and
  `NVIDIA_VISIBLE_DEVICES=all`).
* [`nobookends-sync-cpu.xml`](templates/nobookends-sync-cpu.xml): CPU image,
  no plugin needed.

Until they are in Community Applications, install one by hand:

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/my-NoBookEnds-Sync.xml \
  https://raw.githubusercontent.com/DanCue44/NoBookEnds-Sync/main/templates/nobookends-sync.xml
```

(the URL works once the repository is public; until then copy the file
there), then Docker > Add Container > Template:
NoBookEnds-Sync. Fill in the Audiobookshelf URL (your server IP and the
Audiobookshelf port, for example `http://192.168.1.10:13378`, or
`http://audiobookshelf:80` when both containers share a custom network such as
`proxynet`), the API key and an admin token. Unraid sets `TZ` to the server
time zone automatically, so the auto window follows your local time. Data goes
to `/mnt/user/appdata/nobookends-sync`.

### Audiobookshelf user and API key

The service reads your libraries and downloads audio and EPUB files with its
own Audiobookshelf account. It never changes anything in Audiobookshelf. As an
Audiobookshelf admin:

1. **Settings > Users > Add User**: username `nobookends-sync`, account type
   **User**, a long random password (you will not need it again).
2. Permissions: **Can Download** on; **Can Update**, **Can Delete** and
   **Can Upload** off. Turn on **Can Access Explicit Content** and **Can Access
   All Libraries** (or pick the libraries with audiobooks and EPUBs) and leave
   tags unrestricted, otherwise those books are invisible to the service.
3. **Settings > API Keys > Add API Key** (Audiobookshelf 2.26 or newer): name
   it `nobookends-sync`, no expiry, and set **Act as user** to
   `nobookends-sync`. Copy the key into `ABS_API_KEY`. On older versions use
   that user's API token from Settings > Users instead.

What it calls with that key: `GET /api/libraries`,
`GET /api/libraries/{id}/items`, `GET /api/items/{id}?expanded=1`, and file
downloads from `/api/items/{id}/file/{ino}` (or `/download`) and
`/api/items/{id}/ebook`. Readers never use this key: the app sends each
reader's own Audiobookshelf token, and the service checks with your
Audiobookshelf that the reader can open both the audiobook and the EPUB before
returning anything.

### Reverse proxy and public URL

Phones talk to the service directly, so it needs an HTTPS address that works
away from home, next to your Audiobookshelf address. Use
**`booksync.<your domain>`**: the app looks for `booksync.`, `sync.` and
`nobookends-sync.` next to the Audiobookshelf host (for
`bookshelf.example.com` it tries `booksync.example.com` first), so with that
name readers do not have to type anything. A different address can be entered
in the app, and Allseerr can serve the service under its own address instead
(Settings > NoBookEnds Sync), which needs no proxy host at all.

* **Nginx Proxy Manager**: Proxy Host `booksync.example.com` -> `http`,
  forward host `nobookends-sync` (same Docker network) or the server IP, port
  `8000`; SSL tab: request a Let's Encrypt certificate, Force SSL, HTTP/2.
* **Caddy**:
  ```
  booksync.example.com {
      reverse_proxy nobookends-sync:8000
  }
  ```
* **nginx**: a normal `proxy_pass http://nobookends-sync:8000;` with
  `proxy_set_header Host`, `X-Forwarded-For` and `X-Forwarded-Proto`. No
  websockets. Responses for a whole book can be a few MB (gzip compressed),
  the default buffers are fine.

The alignment endpoints need a valid Audiobookshelf token and the admin
endpoints need `ADMIN_TOKEN`, so exposing the service is safe. If AllSeerr
reaches the service on your LAN, you can also block `/api/v1/admin/` at the
proxy so the admin API is not reachable from the internet at all.

### Speed: GPU or CPU

Transcription is almost all of the work; alignment of a whole book takes
seconds. Times are "x realtime": 10x means a 10 hour audiobook takes 1 hour.

| Hardware | Model | Speed | Source |
| --- | --- | --- | --- |
| NVIDIA Quadro P2000 (Pascal, 5 GB), int8 | `small.en` | **17.6x** (9.1 h book in 29.9 min) | measured, whole book |
| AMD Ryzen 9 5950X, 8 threads, int8 | `small.en` | **3.0x to 3.3x** | measured on a 5 minute clip, server busy (load average about 30) |
| AMD Ryzen 9 5950X, 15 threads, int8 | `small.en` | **7x** | measured, same conditions |
| AMD Ryzen 9 5950X, 4 to 8 threads, int8 | `base.en` | **6x to 26x** | measured, same conditions (wide spread from the load) |
| 4 core desktop or NAS CPU (Intel Core i3/i5, Ryzen 3/5), int8 | `small.en` | about 1.5x to 3x | **estimate** |
| low-power NAS CPU (Intel Celeron/Pentium J or N series) | `small.en` | about 0.5x to 1x | **estimate** |

Any NVIDIA card from Pascal (GTX 10xx, Quadro P series) on should beat a CPU
by a wide margin; VRAM use is about 1 GB with `small.en`. On a CPU, `base.en`
is a good trade: about twice as fast, and the aligner tolerates its extra
mistakes on clear narration. The overnight window (default 01:00 to 06:00)
exists so a CPU install can work through a library without slowing the server
during the day: at 3x a five hour window covers about 15 hours of audio per
night. Books a reader asks for in the app are never held for the window.

### Privacy

Everything stays on your server. Audio, EPUBs, transcripts and alignments are
read from your Audiobookshelf and stored in `/data`; nothing is sent to
NoBookEnds or any other service, and there is no telemetry. The only outbound
connection is the one-time download of the whisper model from Hugging Face
into `/data/models` (set `HF_HUB_OFFLINE=1` after the first run if you want to
forbid even that). Reader tokens are only forwarded to your own
Audiobookshelf to check access, and are cached in memory for a minute.
Synced user data is private to each reader (see [User data](#user-data)).

## How it works

```
ABS library ──(service API key)──> download audio files + EPUB
                                     │
              faster-whisper per audio file (word timestamps, cached on disk)
                                     │
   EPUB spine -> body.textContent per spine item -> sentences -> word tokens
                                     │
     anchors (unique n-grams) -> LIS chain -> recursive anchors -> affine DP
                                     │
     sentence segments: t0/t1, href, cfiStart/cfiEnd, textStart/textEnd, words
                                     │
       /data/results/<audio>__<ebook>.json.gz  ──>  GET /api/v1/alignments
```

* **Auto mode** (default on): every `AUTO_POLL_MINUTES` the service lists ABS
  libraries by `updatedAt`, pairs audiobooks with EPUBs (same item, shared
  ISBN/ASIN, or normalized title + first author surname, across libraries) and
  queues one job per new pair, oldest first, at the lowest priority. On first
  start it backfills every existing pair. A finished pair is re-queued only
  when its audio or EPUB files change (inode, size and mtime of every file).
* **Overnight window**: automatic work (backfill, new pairs, changed files,
  re-alignment after an algorithm update) only starts or resumes inside
  `AUTO_WINDOW`, default `01:00-06:00` in the container's `TZ`. Discovery
  keeps polling all day; only the jobs wait. An auto job still running at the
  end of the window pauses at its next checkpoint (at most 20 minutes of audio
  away; after `AUTO_PAUSE_GRACE_MINUTES` it is stopped anyway) and resumes
  from its transcript checkpoints the next night. `AUTO_WINDOW=always` (or
  empty) turns the window off.
* **On demand**: `POST /api/v1/alignments` queues at user priority and jumps
  ahead of auto jobs at any time of day; a running auto job is stopped and
  goes back to the queue (it resumes from its transcript checkpoints). A user
  request for the auto job that is already running raises its priority, so
  it finishes even if the window closes.
* **Checkpoints.** Transcripts are saved after every 20 minutes of audio, so a
  paused, preempted or crashed job resumes close to where it stopped, also
  inside a long single-file audiobook.
* **One job at a time.** Each job runs in a spawned child process, so GPU
  memory is held only while a job runs. The API process never touches CUDA.
* **Transcribe once.** Transcripts are cached per audio file and model under
  `/data/transcripts`. Reads never trigger work. When the alignment algorithm
  improves (`ALGO_VERSION` bump), stored results are re-aligned from cached
  transcripts in seconds, and the old result is served until the new one is
  ready.

### Alignment

1. Book and transcript are tokenized the same way (letters/digits, apostrophes
   kept inside words then dropped, accents folded, lowercase).
2. **Anchors:** 6-grams that occur exactly once in the transcript and exactly
   once in the book are matched; the longest chain increasing in both
   sequences (LIS) is kept. Coincidental matches cannot beat the long run of
   true matches, and abridged or skipped sections simply become jumps.
3. **Recursion:** between anchors, 4-grams then 3-grams that are unique within
   that smaller region become new anchors, until regions are small.
4. **DP:** small regions get a Gotoh (affine gap) alignment with exact and
   fuzzy word matches (Levenshtein >= 0.75). Book gaps are cheap to extend
   (abridgement), transcript gaps cost more (narrator asides, ASR insertions).
   The open ends before the first and after the last anchor use free end
   gaps, so intros, credits and outros fall out as gaps.
5. Tiny islands of matches far from any other match are dropped.
6. Matched book words take the transcript word times; up to 12 unmatched words
   between matches are interpolated by character length. A sentence becomes a
   segment when at least 40% of its words matched (or all were interpolated);
   everything else is a gap with no segment.

Quality is reported per job (`quality` in the status object and admin view):
`score` = matched transcript words / all transcript words, `speechCoverage` =
share of transcribed speech time that falls inside segments, `bookCoverage` =
share of book words inside segments (low for abridged editions).

### Kindle ebooks (AZW3 and MOBI)

An audiobook whose only ebook is an AZW3 or MOBI file can use Listen & Read
too: the service converts the file to EPUB (in Python, no Calibre needed),
aligns against that EPUB and serves the same copy to the app at
`GET /api/v1/ebooks/{ebookItemId}/epub`, so the CFIs in the alignment match
what the reader shows. The copy is cached under `/data/converted`, one per
ebook file; a changed file, or a newer converter, makes a new copy and
re-aligns the book. When an audiobook matches both an EPUB and a Kindle file,
the EPUB is used.

KF8 (AZW3, and the KF8 half of a combined MOBI) and older Mobipocket files
are read, uncompressed or compressed. Files with DRM (bought from a store)
cannot be converted. Errors use the `reason` field:

| Status | `reason` | Meaning |
| --- | --- | --- |
| 404 | `no_ebook` | the item has no ebook file |
| 415 | `not_kindle` | the ebook is not AZW3 or MOBI (read it from Audiobookshelf) |
| 422 | `drm_protected` | the file has DRM |
| 422 | `unreadable_ebook` | the file could not be converted |
| 413 | `too_large` | the file is over 512 MB |
| 503 | `kindle_disabled` | `KINDLE_EBOOKS=false` |

The download uses the reader's own token, so their Audiobookshelf access
applies, as for the alignment endpoints.

### CFIs and offsets

For every spine item the service builds `body.textContent` (all text nodes
under `<body>` in document order, raw whitespace included, comments excluded)
and remembers for each text node its CFI location. Each segment carries both:

* `href`: the spine item's manifest href, relative to the OPF, exactly as in
  the manifest (what epub.js uses as a section href).
* `cfiStart` / `cfiEnd`: point CFIs of the sentence's first character and of
  the position just after its last character, e.g.
  `epubcfi(/6/26[id1132]!/4/8/2/1:0)`. Element steps carry `[id]` assertions
  when the element has an id. A comment inside a text run does not start a
  new text chunk (offsets continue across it).
* `textStart` / `textEnd` (extension): offsets of the sentence in that spine
  item's `body.textContent`.
* `text`: the textContent slice with each whitespace character replaced by a
  space (length preserving, so `textStart + words[i].start` is still a valid
  textContent offset).
* `words[].start` / `words[].end`: offsets of each word within `text`.

All offsets (CFI character offsets, `textStart/End`, word `start/end`) are
**UTF-16 code units**, matching JavaScript strings and the DOM. To highlight a
word in a WebView: walk `document.body` text nodes with a TreeWalker,
accumulate lengths until `textStart + start`, and build a Range.

## User data

The app stores a few kinds of per-reader data here, because Audiobookshelf
cannot hold them:

| Collection | What |
| --- | --- |
| `podcastSubscriptions` | podcasts a reader subscribed to in the app (the feed URL and settings; episodes come straight from the RSS feed, the server stores no audio) |
| `podcastProgress` | listening position per episode of those podcasts |
| `ratings` | the reader's ratings |
| `highlights` | ebook highlights and notes |
| `clips` | audio clips and bookmarks |
| `readingState` | the last mode per book (listen, read, or Listen & Read) behind the app's Continue card (1.3.0) |
| `appSettings` | the app's settings (not credentials), one record per account, when settings sync is on (1.4.0) |
| `achievements` | earned Shelf badges and celebrated milestones (1.5.0) |
| `listeningPaths` | the chosen path or custom chapter order for multi-path audiobooks (1.6.0) |

* **Per reader, never shared.** Every record is keyed by the Audiobookshelf
  user id of the token that wrote it. Readers only ever see their own data;
  there is no sharing between users and no admin endpoint that returns record
  contents (the admin status only counts records). Tokens and record contents
  are never logged.
* **Wipe.** `DELETE /api/v1/me/data` deletes all of the caller's data in every
  collection at once (the app offers it in its privacy settings).
* **No GPU, no worker.** User data is plain SQLite in `/data/sync.db`. It works
  on the CPU image and with alignment switched off (`ALIGNMENT_ENABLED=false`).
* **Sync model.** Each record is a small JSON object (at most 64 KB) with a
  client id and a client `updatedAt`; the newest `updatedAt` wins. Deletes are
  kept as tombstones for 90 days so every device learns about them, then
  purged.

## API (contract v1)

All alignment endpoints need `Authorization: Bearer <the user's ABS access
token>`. The service validates it with `GET /api/me` on its ABS and checks the
user can fetch both items (results cached for `AUTH_CACHE_SEC`, default 60 s).
Missing/invalid token: 401. Item missing or not accessible: 404 (or 403 as
ABS reports it). ABS unreachable: 502.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/v1/health` | public: `{ ok, version, gpu }` |
| GET | `/api/v1/capabilities` | public: `{ version, alignment, userData, collections, kindleEbooks, kindleFormats }` |
| GET | `/api/v1/alignments/{audioItemId}?ebookItemId=...[&from=sec&to=sec]` | status object with segments; `ETag`, `If-None-Match` -> 304 |
| POST | `/api/v1/alignments` | body `{ audioItemId, ebookItemId }`; idempotent; status object without segments |
| GET | `/api/v1/ebooks/{ebookItemId}/epub` | an AZW3 or MOBI ebook as the EPUB its alignment uses (see Kindle ebooks); `ETag`, `If-None-Match` -> 304 |

Status object:

```json
{
  "status": "ready",
  "progress": 1.0,
  "version": 1,
  "audioItemId": "993512c3-...",
  "ebookItemId": "efa5b32c-...",
  "durationSec": 32679.671,
  "quality": { "score": 0.983, "speechCoverage": 0.971, "bookCoverage": 0.325 },
  "segments": [
    {
      "id": 412,
      "t0": 48.514,
      "t1": 49.914,
      "href": "CR!BJHPA831ES3B3F7WSRKZ17DMGSM7_split_011.html",
      "cfiStart": "epubcfi(/6/26[id1132]!/4/8/2/1:0)",
      "cfiEnd": "epubcfi(/6/26[id1132]!/4/8/2/1:24)",
      "text": "How This Book Came to Be",
      "textStart": 31,
      "textEnd": 55,
      "words": [ { "t0": 48.514, "t1": 48.614, "start": 0, "end": 3 } ]
    }
  ]
}
```

* `status`: `none` (never requested), `queued`, `processing`, `ready`,
  `failed`. Segments are present (possibly empty) on GET, absent on POST.
* `progress` 0..1 while processing (transcription is 95%, alignment the rest).
* Segments are sentence level, ordered by time; times are seconds from the
  start of the whole book across files. Unmatched text has no segment.
* `id` is the sentence index in the EPUB (stable for a given EPUB and
  algorithm version).
* Windows: `from`/`to` return the segments overlapping `[from, to]`.
* Extensions (additive): `quality`, `error` (failed), `queuePosition`
  (queued), `updating: true` + `updateProgress` (a re-run is in progress and
  the previous result is being served), `processedSec` (a duration-limited
  test run), `textStart`/`textEnd` per segment.
* POST on a failed job re-queues it only after `FAILED_RETRY_AFTER_SEC`
  (default 24 h); admins can force a re-run.

Examples:

```bash
SYNC=https://sync.example.com
TOKEN=...   # the user's ABS access token
curl -s $SYNC/api/v1/health
curl -s -X POST $SYNC/api/v1/alignments -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"audioItemId":"993512c3-8d72-43d3-93ee-32715d25b889","ebookItemId":"efa5b32c-faba-45e2-b8b2-030e5579bdaa"}'
curl -s "$SYNC/api/v1/alignments/993512c3-8d72-43d3-93ee-32715d25b889?ebookItemId=efa5b32c-faba-45e2-b8b2-030e5579bdaa&from=0&to=600" \
  -H "Authorization: Bearer $TOKEN" --compressed -D - -o window.json
```

Responses over 1 KB are gzip-compressed when the client sends
`Accept-Encoding: gzip`.

### Capabilities

`GET /api/v1/capabilities` (no auth) tells the app what this server offers:

```json
{ "version": "1.7.0", "alignment": true, "userData": true,
  "collections": ["podcastSubscriptions", "podcastProgress", "ratings", "highlights", "clips",
                  "readingState", "appSettings", "achievements", "listeningPaths"],
  "kindleEbooks": true, "kindleFormats": ["azw3", "azw", "mobi", "kf8", "prc"] }
```

`kindleEbooks` is false when `KINDLE_EBOOKS=false`. Servers older than 1.7.0
leave both fields out.

`alignment` is false when the alignment worker is switched off
(`ALIGNMENT_ENABLED=false`); `POST /api/v1/alignments` then answers `503`
with `reason: "alignment_disabled"` (stored results are still served).

### User data (v1, 2026-10-14)

All `/api/v1/me/*` endpoints need `Authorization: Bearer <the user's ABS
access token>`, checked with `GET /api/me` on ABS (cached for
`AUTH_CACHE_SEC`). Invalid token: 401. Data is keyed by the ABS user id, so
every token of the same ABS user sees the same data. Collections are fixed:
`podcastSubscriptions`, `podcastProgress`, `ratings`, `highlights`, `clips`,
`readingState` (1.3.0), `appSettings` (1.4.0), `achievements` (1.5.0), `listeningPaths` (1.6.0); any other name is 404. Apps check `collections` in the
capabilities, so an older server simply leaves a newer collection on the
device (or iCloud).

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/v1/me/data/{collection}?since=<seq>` | changes after `since` (default 0: everything), oldest first, 1000 per page |
| POST | `/api/v1/me/data/{collection}` | up to 500 writes |
| DELETE | `/api/v1/me/data` | wipe all of the caller's data, `{ "deleted": <records removed> }` |

Read:

```json
{
  "items": [
    { "id": "b7d1", "data": { "stars": 4 }, "updatedAt": 1760400000000, "deleted": false, "seq": 41 },
    { "id": "c02e", "data": null, "updatedAt": 1760400005000, "deleted": true, "seq": 42 }
  ],
  "seq": 42,
  "more": false
}
```

Store the returned `seq` and send it as `since` next time. With `more: true`
call again right away with the returned `seq` (the last item's). Without
`more`, `seq` is the latest change in that collection for this user (never
less than `since`). `seq` is one counter per user across all collections, so
numbers can skip within one collection.

Write:

```json
{ "items": [
  { "id": "b7d1", "data": { "stars": 5 }, "updatedAt": 1760400100000 },
  { "id": "c02e", "updatedAt": 1760400105000, "deleted": true }
] }
```

```json
{ "applied": ["b7d1"], "rejected": [{ "id": "c02e", "reason": "stale" }], "seq": 43 }
```

* `id`: 1 to 200 characters, chosen by the client. `updatedAt`: integer ms
  since the epoch (client clock). `data`: a JSON object, required unless
  `deleted` is true (ignored for deletes). `deleted` defaults to false.
* Last write wins by `updatedAt`. A write with an `updatedAt` older than or
  equal to the stored one is rejected as `stale` and the stored record is
  kept; the client picks up the stored version on its next GET.
* Every applied write gets a new `seq`. Writes in one request are applied in
  order, in one transaction; the answer's `seq` is the latest change in that
  collection for this user.
* Deletes become tombstones (`deleted: true`, `data: null`), also for ids the
  server has never seen, and are purged 90 days after the server received
  them. A client that has not synced a collection for 90 days should read it
  again from `since=0` and replace its local copy.
* Rejection reasons: `stale`, `too_large` (`data` over 64 KB as compact
  JSON), `quota` (the collection already holds 50,000 live records for this
  user; updates and deletes still work), `invalid` (bad `id`, `updatedAt`,
  `data` or `deleted`; `id` is `null` when the item was not an object).
* More than 500 items: `413` with `reason: "too_many_items"` and nothing
  applied. A request body over 8 MB on any endpoint: `413`. A body without an
  `items` list: `422`.
* After a wipe, other devices still hold their copies and cursors; the app
  should reset them (`since=0` and clear local data) when the reader wipes.

### Admin (`Authorization: Bearer $ADMIN_TOKEN`; disabled when unset)

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/v1/admin/status` | cheap summary: version, GPU, window, queue counts, current job, user data counts |
| GET | `/api/v1/admin/jobs[?status=]` | queue, worker, discovery state, settings, per-job quality and stats |
| GET | `/api/v1/admin/jobs/{audioId}/{ebookId}` | one job |
| POST | `/api/v1/admin/jobs/{audioId}/{ebookId}/rerun` | body `{ "maxDurationSec": 1800 \| null }`; forces a re-run at admin priority |
| POST | `/api/v1/admin/alignments` | queue one pair or up to 500 pairs without a user token (below) |
| GET | `/api/v1/admin/pairs` | discovered audio/ebook pairs with titles and job status |
| POST | `/api/v1/admin/discovery/run[?full=true]` | poll ABS now |
| POST | `/api/v1/admin/realign-outdated` | re-align results from older algorithm versions |

#### Queueing pairs: `POST /api/v1/admin/alignments`

This is what integrations such as AllSeerr call. It is idempotent: a pair that
is queued or running is left alone (its priority can only go up), a ready
result is not redone (unless `force`, or it was made by an older algorithm),
and a failed pair is retried only with `force` (or, at user or admin
priority, after `FAILED_RETRY_AFTER_SEC`).

Single pair:

```json
{
  "audioItemId": "993512c3-...",
  "ebookItemId": "efa5b32c-...",
  "priority": "user",
  "source": "allseerr",
  "maxDurationSec": null,
  "force": false
}
```

* `priority` (optional): omitted = admin priority (runs before everything,
  the original behavior); `"user"` = same as a reader's request: ahead of
  automatic work and never held for the overnight window (use it when someone
  just asked for that book); `"auto"` = joins the automatic queue and only
  runs inside `AUTO_WINDOW` (use it for bulk backfills).
* `source` (optional, `[A-Za-z0-9_.-]`, up to 40 characters): shown in the
  admin job list as `admin:<source>`.
* `maxDurationSec`, `force` (optional, single pair only): test runs and forced
  re-runs.

Before queueing, both items are fetched from ABS with the service key. The
answer is the admin job view (`200`), or an error with a reason and nothing
queued:

| Status | `reason` | When |
| --- | --- | --- |
| 404 | `not_found` | an item does not exist (or the service user cannot see it) |
| 422 | `no_audio` | the audio item has no audio files |
| 422 | `no_epub` | the ebook item has no EPUB (no ebook, or PDF/CBZ/...) |
| 422 | `missing_ids`, `ambiguous`, `too_many_pairs` | malformed body |
| 502 | `abs_auth` / `abs_unavailable` | ABS rejected the service key / could not be reached |

```json
{ "detail": "ebookItemId has no EPUB", "reason": "no_epub" }
```

Successful single answer (admin job view, abridged):

```json
{
  "audioItemId": "993512c3-...", "ebookItemId": "efa5b32c-...",
  "status": "queued", "progress": 0.0, "priority": 10, "source": "admin:allseerr",
  "waitingForWindow": false, "error": null, "quality": null, "attempts": 0,
  "titles": { "audio_id": { "title": "Steve Jobs", "author": "Walter Isaacson" } }
}
```

`priority` is 0 (auto), 10 (user) or 20 (admin). `waitingForWindow` is true
for a queued auto job while the window is closed.

Many pairs (at most 500 per call; each item is fetched once per call):

```json
{ "priority": "auto", "source": "allseerr", "pairs": [
  { "audioItemId": "a1", "ebookItemId": "e1" },
  { "audioItemId": "a2", "ebookItemId": "e2" }
] }
```

```json
{
  "accepted": 1,
  "rejected": 1,
  "results": [
    { "audioItemId": "a1", "ebookItemId": "e1", "ok": true, "status": "queued", "priority": 0,
      "waitingForWindow": true },
    { "audioItemId": "a2", "ebookItemId": "e2", "ok": false, "reason": "no_epub",
      "error": "ebookItemId has no EPUB" }
  ]
}
```

The bulk call answers `200` even when some pairs are rejected (or `422
too_many_pairs`). Results are in request order.

#### `GET /api/v1/admin/status`

```json
{
  "version": "1.6.0",
  "gpu": true,
  "window": { "start": "01:00", "end": "06:00", "active": false },
  "queue": { "user": 0, "auto": 12, "total": 12 },
  "current": { "audioItemId": "993512c3-...", "ebookItemId": "efa5b32c-...", "title": "Steve Jobs",
               "progress": 0.4213 },
  "userData": {
    "users": 3,
    "collections": {
      "podcastSubscriptions": { "items": 41, "deleted": 2, "users": 2 },
      "podcastProgress": { "items": 980, "deleted": 0, "users": 2 },
      "ratings": { "items": 120, "deleted": 4, "users": 3 },
      "highlights": { "items": 0, "deleted": 0, "users": 0 },
      "clips": { "items": 7, "deleted": 1, "users": 1 },
      "readingState": { "items": 64, "deleted": 3, "users": 2 },
      "appSettings": { "items": 2, "deleted": 0, "users": 2 },
      "achievements": { "items": 9, "deleted": 0, "users": 2 },
      "listeningPaths": { "items": 1, "deleted": 0, "users": 1 }
    }
  }
}
```

`window.start`/`end` are `null` and `active` is `true` when the window is off
(`AUTO_WINDOW=always`). `queue` counts queued jobs only: `user` includes admin
priority, `auto` is automatic work. `current` is `null` when idle.
`userData` counts records only, never their contents: `items` are live
records, `deleted` tombstones, `users` readers with at least one live record
(top-level `users`: in any collection).

## Configuration

| Env | Default | Meaning |
| --- | --- | --- |
| `ABS_URL` | `http://audiobookshelf:80` | ABS base URL as seen from the container |
| `ABS_API_KEY` | | API key of a read-only ABS user with download permission |
| `ADMIN_TOKEN` | | enables the admin endpoints |
| `ALIGNMENT_ENABLED` | `true` | `false`: no alignment worker or auto mode (user data and stored results are still served) |
| `WHISPER_MODEL` | `small.en` | `tiny.en`, `base.en`, `small.en`, `medium.en`, `distil-large-v3`, ... |
| `WHISPER_DEVICE` | `auto` (`cpu` in the CPU image) | `auto` (CUDA if present), `cuda`, `cpu` |
| `WHISPER_COMPUTE_TYPE` | `int8` | GPU compute type; `int8` or `int8_float32` on Pascal (no fast FP16) |
| `WHISPER_CPU_COMPUTE_TYPE` | `int8` | used on CPU and for the CPU fallback |
| `WHISPER_BEAM_SIZE` | `1` | greedy decoding; 5 is slightly more accurate and about 2x slower |
| `WHISPER_CPU_THREADS` | `4` (`auto` in the CPU image) | CTranslate2 CPU threads; `auto` or `0`: the `--cpus` limit if set, else half the logical CPUs, at most 16 |
| `WHISPER_LANGUAGE` | `en` | for multilingual models |
| `WHISPER_VAD` | `true` | Silero VAD skips silence/music (faster, fewer hallucinations) |
| `KINDLE_EBOOKS` | `true` | convert AZW3 and MOBI ebooks for Listen & Read (see Kindle ebooks) |
| `MAX_AUDIO_SEC` | `0` | limit every job to the first N seconds (testing); 0 = whole book |
| `AUTO_ALIGN` | `true` | auto mode |
| `AUTO_POLL_MINUTES` | `10` | poll interval (discovery runs all day) |
| `AUTO_WINDOW` | `01:00-06:00` | local-time window for automatic jobs; may cross midnight (`22:30-05:00`); `always` or empty: no window. User and admin requests ignore it |
| `AUTO_PAUSE_GRACE_MINUTES` | `15` | an auto job that has not reached a checkpoint this long after the window closed is stopped (it resumes from its last checkpoint) |
| `TZ` | container default (UTC) | time zone for `AUTO_WINDOW`, e.g. `America/Chicago` (Unraid sets it automatically) |
| `AUTO_FULL_RESYNC_HOURS` | `6` | full listing (notices removed items) |
| `AUTO_MAX_EBOOKS_PER_AUDIO` | `3` | cap on ebook matches per audiobook |
| `FAILED_RETRY_AFTER_SEC` | `86400` | user POST retry window for failed jobs |
| `AUTH_CACHE_SEC` | `60` | token and item access cache |
| `DATA_DIR` | `/data` | state, results, transcripts, models (`/data/models`), temp files |

### Model trade-off

Alignment needs the right words, not perfect punctuation, so a small model is
enough: the aligner tolerates misheard words. `small.en` int8 is the default
(0.5 to 0.9 GB VRAM). `base.en` is roughly 2x faster with more errors (still
aligns well on clear narration); `medium.en` or `distil-large-v3` are more
accurate on difficult audio but slower and need more VRAM (1 to 1.5 GB int8).
On the Quadro P2000 (Pascal, compute 6.1) use `int8` or `int8_float32`; FP16
is slow on Pascal. Without a GPU the service falls back to CPU.

## Building from source on Unraid (owner's deployment)

The published images are the normal way to install. To build from a checkout
on the server instead:

```bash
docker build --force-rm -t nobookends-sync:dev .     # never DOCKER_BUILDKIT=1 on Unraid
```

Secrets go in an env file that only root can read:

```bash
mkdir -p /mnt/user/appdata/NoBookEnds-Sync
cat > /mnt/user/appdata/NoBookEnds-Sync/nobookends-sync.env <<'EOF'
ABS_URL=http://audiobookshelf:80
ABS_API_KEY=...
ADMIN_TOKEN=...
EOF
chmod 600 /mnt/user/appdata/NoBookEnds-Sync/nobookends-sync.env
```

Run:

```bash
docker run -d --name NoBookEnds-Sync \
  --network proxynet \
  --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all \
  --env-file /mnt/user/appdata/NoBookEnds-Sync/nobookends-sync.env \
  -e WHISPER_MODEL=small.en -e WHISPER_COMPUTE_TYPE=int8 -e WHISPER_BEAM_SIZE=1 \
  -e TZ=America/Chicago -e AUTO_WINDOW=01:00-06:00 \
  -v /mnt/user/appdata/NoBookEnds-Sync:/data \
  -l net.unraid.docker.managed=dockerman \
  --restart unless-stopped \
  nobookends-sync:dev
```

Port 8000 inside the container (no host port needed; expose it through Nginx
Proxy Manager on proxynet as `http://NoBookEnds-Sync:8000`). `--gpus all` works
too where the NVIDIA runtime is not the Unraid default.

Pass `TZ`: a container started with `docker run` (not from an Unraid
template) gets no `TZ` and would read `AUTO_WINDOW` as UTC. From 1.1.0 the
window is on by default, so after redeploying, automatic work only runs from
01:00 to 06:00; reader requests are unaffected.

### Measured on a Quadro P2000 (Steve Jobs, Walter Isaacson, abridged, 9.1 h, 126 files)

* `small.en`, int8, beam 1, VAD on: **17.6x realtime** (8.6 h transcribed in
  29.3 min; whole job 29.9 min including downloads and alignment). Alignment
  of the full book: 7.5 s. Re-alignment from cached transcripts: 13 s.
* VRAM: about 0.5 GB at the start of a job, peak 0.93 GB; nothing is held
  between jobs. GPU utilization about 90% while a job runs.
* Quality: score 0.983 (74,515 of 75,807 transcript words matched),
  speech coverage 97.1%, 4,608 sentence segments, book coverage 32.5% (the
  audiobook is abridged; skipped text correctly has no segments).
* Spot checks (independent `base.en` transcription of 10 s around `t0` of
  random segments): every sampled segment's text was what was being read;
  the sentence's first words were heard within 0.5 s of `t0` in 37 of 38 and
  48 of 56 checks (two samples), within 1 s in all of them, median offset
  0.14 to 0.16 s.
* Result size: 5.5 MB JSON for the whole book, 1.1 MB gzipped; use `from`/`to`
  windows for incremental loading.

## Development

```bash
make venv     # python3 -m venv .venv + requirements-dev.txt
make test     # pytest: aligner, EPUB/CFI mapping, auth, API contract, user data, discovery, pipeline
make run      # local server with DATA_DIR=./data
make fixture  # rebuild tests/fixtures/alice.epub (public-domain Alice excerpt)
```
