# YouTube Shorts Clipper

Follow a YouTube channel to turn new uploads into Shorts, or paste a batch of video links and choose how many clips to make. Each clip gets speech-synchronized captions and a custom portrait thumbnail. The worker uploads the MP4, waits for YouTube to finish processing it, and applies the thumbnail.

## How it works

**You can run the full pipeline without an AI API key or a paid AI subscription.** GitHub stores the code. A running Windows, macOS, or Linux computer executes the worker.

| Step | What happens | What it needs |
| --- | --- | --- |
| Find videos | Follow checks a public channel feed every 60 seconds. Links mode takes a list once and distributes your requested count across the videos. | Internet for YouTube sources. You can also process your own local files. |
| Transcribe | Faster-whisper runs the multilingual Whisper base model on your CPU. It detects the spoken language and timestamps individual words. | A one-time model download. Local transcription is free of per-minute API charges. A matching SRT file skips transcription. |
| Select clips | The worker scores passages using sentence boundaries, pauses, continuous speech, and your duration limit. It selects distinct passages and reports a shortfall when the material is too short. | Local Python. Selection uses the transcript and timing. |
| Render | FFmpeg reframes the video, burns in the captions, and makes a portrait JPEG from a frame inside that passage. The title and short description use words spoken in the clip. | FFmpeg with libass, disk space for cached sources and rendered clips, and CPU time. Longer inputs take more time. |
| Post | The worker sends each MP4 to the channel you authorized. After YouTube finishes processing, it attaches the JPEG. Progress is saved so you can restart. | Internet, a Google Cloud project with YouTube Data API v3 enabled, Desktop OAuth authorization, and a channel eligible for custom thumbnails. API quota applies. [YouTube quota costs](https://developers.google.com/youtube/v3/determine_quota_cost) |

Running on your own laptop uses its electricity, storage, and internet connection. The software has no subscription or per-clip fee. Local previews work without connecting Google. Keep the computer awake for continuous channel following. Hosting it on your own server is optional.

## Quick start

1. Clone or download this repository on a computer that can stay on. Install Python 3.11 or 3.12 and FFmpeg with libass. Both `ffmpeg` and `ffprobe` should be available in a terminal. [Python downloads](https://www.python.org/downloads/) · [FFmpeg downloads](https://ffmpeg.org/download.html)
2. Open a terminal in the folder containing `shorts.py`. Create and activate a virtual environment using the commands for your system below.
3. Run `python -m pip install -r requirements.txt` in that environment.

**macOS or Linux**

~~~sh
python3 -m venv .venv
. .venv/bin/activate
~~~

**Windows Command Prompt**

~~~bat
py -3.12 -m venv .venv
.venv\Scripts\activate.bat
~~~

### Connect the destination channel

1. Create or select a Google Cloud project and enable **YouTube Data API v3**. [API library](https://console.cloud.google.com/apis/library/youtube.googleapis.com)
2. In Google Auth Platform, configure **Branding** and **Audience**. If you use an External app in Testing, add your Google account as a test user. Under **Data Access**, add `https://www.googleapis.com/auth/youtube.upload` for posting videos and covers, and `https://www.googleapis.com/auth/youtube.readonly` to check processing status. [Google Auth Platform](https://console.cloud.google.com/auth/overview)
3. Under **Clients**, create a **Desktop app** OAuth client. Download its JSON file to this folder as `client_secret.json`. [OAuth clients](https://console.cloud.google.com/auth/clients)
4. Run the command below on your computer. In the browser, select the YouTube channel that should receive the Shorts and grant both permissions. If you connected an earlier upload-only version, run this once again.

~~~sh
python shorts.py connect client_secret.json
~~~

Google restricts uploads from some unaudited API projects to private visibility. If you need public Shorts, check your project's YouTube API audit status. An External OAuth app in Testing can also receive refresh tokens that expire after seven days. [YouTube upload requirements](https://developers.google.com/youtube/v3/docs/videos/insert) · [Google OAuth token expiration](https://developers.google.com/identity/protocols/oauth2#expiration)

### Open Shorts Studio

~~~sh
python shorts.py
~~~

**Paste links** accepts a multiline paste. Set **Clip count**, then choose **Whole batch** or **Each video**. **Follow channel** accepts a channel URL, handle, or channel ID and applies your clip count to each new upload. Pick a caption style and destination, then start. **Save locally** lets you preview the MP4 and JPEG before connecting a channel. Keep Studio open while following. On headless machines, use the commands below. Linux desktop installs may need the Python Tk package.

### Paste a batch in the terminal

~~~sh
python shorts.py links --clips 6 --publish public
~~~

Paste the links, then press Enter on an empty line. This requests **six Shorts in total**. The worker shares slots across the sources and uses spare capacity from longer videos. Repeated links to the same YouTube video count once. To request **two clips from each video** or run from a saved list, use the following command with one URL per line in the file.

~~~sh
python shorts.py links --file links.txt --clips 2 --per-video --publish public
~~~

The batch saves its selected time ranges before posting. Rerunning the same inputs and count resumes the batch. If the available speech cannot supply enough distinct passages, the summary reports the actual number and the command returns a nonzero exit status.

### Follow new uploads

To publish from a local folder, run the following command. The worker creates `inbox` on first use. Drop finished videos there. It also picks up files already in that folder.

~~~sh
python shorts.py watch inbox --publish public
~~~

To publish from new uploads on a source channel, replace the example handle below with its YouTube handle, channel URL, or channel ID. The first scan records the current feed. Subsequent scans process new arrivals. Add `--include-latest` on the first scan to process the most recent existing video immediately. The worker recognizes its own recorded uploads when the source and destination are the same channel. [Find your channel ID](https://www.youtube.com/account_advanced)

~~~sh
python shorts.py follow "@YourChannel" --clips 2 --publish public
~~~

Leave the worker running. It checks every 60 seconds. The first spoken video downloads and caches a Whisper model. For a local preview, run `python shorts.py watch inbox`. The preview works before connecting YouTube. `Ctrl+C` stops the worker.

## What each Short contains

- A 1080 × 1920 H.264 video with AAC audio. The default layout keeps the full source picture over a blurred background. `--layout crop` fills the screen with a center crop.
- A 1080 × 1920 JPEG cover. Each uses an uncaptioned frame from inside the selected passage with a dark gradient and a headline drawn from the clip. YouTube requires the video to finish processing before a custom Shorts thumbnail can be applied. The channel must have custom-thumbnail access. [YouTube thumbnail API](https://developers.google.com/youtube/data/reference/rest/v3/thumbnails/set)
- Captions embedded in the picture, with the spoken word highlighted. The worker also saves a matching UTF-8 `captions.srt`. To use your own subtitles, put `recording.srt` beside `recording.mp4` before moving the video into the inbox. Supplied subtitles use phrase-level captions.
- A title drawn from a question or a short sentence in the clip. A second excerpt and the source link go in the description when available. The CLI starts at one clip and Studio starts at three. The default maximum length is 35 seconds.

[View an example thumbnail generated from an original narrated clip.](examples/thumbnail.jpg)

The output is under `shorts-output/clips/`. Each folder contains `short.mp4`, `thumbnail.jpg`, `captions.srt`, and `details.json` with the title and source time range. A successful upload prints its YouTube Shorts link. The worker confirms when it has attached the cover.

## Controls

| Setting | Effect |
| --- | --- |
| `--clips 3 --seconds 45` | Request three distinct passages up to 45 seconds each. In links mode the count covers the whole batch unless you add `--per-video`. Duration accepts 5–180 seconds. |
| `--caption-style pop` or `classic` | Pop uses larger outlined text and a brief pulse on the highlighted word. Classic uses a steady highlight. SRT inputs use phrase-level timing. |
| `--publish private`, `unlisted`, or `public` | Set upload visibility. Omitting this option saves local clips. Change the visibility of an existing upload in YouTube Studio. |
| `--audience kids` or `--audience general` | Set the made-for-kids declaration. Omitting the option leaves the choice to YouTube's channel settings. |
| `--language en` | Set the spoken language for transcription. The default detects it automatically. Captions follow the spoken language. |
| `--model small` | Use another faster-whisper model. The default is the multilingual `base` CPU model. A downloaded model directory also works. |
| `--interval 300` | Check the source every five minutes. |
| `--once` | Scan once and exit. Use with Task Scheduler, launchd, or cron on a computer with the same output folder and credentials. |
| `--output PATH` | Set the directory for clips, cached inputs, the feed queue, and upload receipts. Reuse it across restarts. |

A single file or a single YouTube video URL also works with `run`.

~~~sh
python shorts.py run "recording.mp4" --clips 2
python shorts.py run "https://www.youtube.com/watch?v=VIDEO_ID" --publish unlisted
~~~

Use source material you can republish. YouTube imports need a publicly accessible video and network access to YouTube. Some formats also need Deno or a current Node.js runtime for yt-dlp. Local originals can go straight into the inbox. Channel discovery uses YouTube's recent public feed, so an extended outage can miss uploads that have already rolled out of that feed.

## Run in the background

On Linux, the included user service runs the folder watcher and requests **public** uploads. Install the project at `~/shorts-clipper`, complete the setup there, then run these commands. The service uses the virtual environment and channel authorization for your user.

~~~sh
mkdir -p ~/.config/systemd/user
cp shorts-clipper.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now shorts-clipper.service
journalctl --user -u shorts-clipper.service -f
~~~

The user service starts with your login. On an always-on server configured for user lingering, it can also run between logins. To stop and disable it, run `systemctl --user disable --now shorts-clipper.service`.

For a periodic task on another OS, point the scheduler at the virtual environment's Python executable. Set its working directory to the project folder and pass `shorts.py watch inbox --once --publish public` as arguments.

## Restarts and uploads

Keep `shorts-output/.state` with the project. It holds the feed queue and per-clip upload receipts. The worker resumes interrupted uploads and skips previously uploaded clips with the same source content and time range. One worker uses an output directory at a time. Use a separate output directory when switching destination channels.

The video ID is saved before thumbnail processing starts. If processing takes longer than the polling window or the thumbnail request fails, rerun the same job to retry the cover on the existing video. The continuous watcher retries automatically.

If YouTube accepts a final chunk and its response is lost, the worker asks the existing upload session for the result. If that session has expired, it pauses that clip. Find the title in YouTube Studio, then run one of the following commands using the generated clip folder. Use `--retry` only after confirming the clip is absent from Studio.

~~~sh
python shorts.py reconcile "shorts-output/clips/CLIP_FOLDER" --video-id EXISTING_ID
python shorts.py reconcile "shorts-output/clips/CLIP_FOLDER" --retry
~~~

The Google token is stored under your user configuration directory in `shorts-clipper/youtube.json`. Set `SHORTS_CLIPPER_TOKEN` to choose a different token file. Keep the token, client JSON, and upload session state private.

To run the regression checks, use `python -m unittest discover -s tests -v` in the activated environment.
