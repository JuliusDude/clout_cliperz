# YouTube Shorts Clipper

Give the worker a folder or a public YouTube channel to watch. For each new video, it transcribes the speech, picks a highlight, renders a vertical MP4 with captions, and uploads it to your connected YouTube channel. You set up the source once.

## Quick start

1. Unzip the project on a computer that can stay on. Install Python 3.11 or 3.12 and FFmpeg with libass. Both `ffmpeg` and `ffprobe` should be available in a terminal. [Python downloads](https://www.python.org/downloads/) · [FFmpeg downloads](https://ffmpeg.org/download.html)
2. Open a terminal in the extracted `shorts-clipper` folder. Create and activate a virtual environment using the commands for your system below.
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
2. In Google Auth Platform, configure **Branding** and **Audience**. If you use an External app in Testing, add your Google account as a test user. Under **Data Access**, add the YouTube upload scope, `https://www.googleapis.com/auth/youtube.upload`. [Google Auth Platform](https://console.cloud.google.com/auth/overview)
3. Under **Clients**, create a **Desktop app** OAuth client. Download its JSON file to this folder as `client_secret.json`. [OAuth clients](https://console.cloud.google.com/auth/clients)
4. Run the command below on your computer. In the browser, select the account and YouTube channel that should receive the Shorts, then grant upload access. The worker saves permission locally for subsequent runs.

~~~sh
python shorts.py connect client_secret.json
~~~

Google restricts uploads from some unaudited API projects to private visibility. If you need public Shorts, check your project's YouTube API audit status. An External OAuth app in Testing can also receive refresh tokens that expire after seven days. [YouTube upload requirements](https://developers.google.com/youtube/v3/docs/videos/insert) · [Google OAuth token expiration](https://developers.google.com/identity/protocols/oauth2#expiration)

### Start the automation

To publish from a folder, run the following command. The worker creates `inbox` on first use. Drop finished videos there. It also picks up files already in that folder.

~~~sh
python shorts.py watch inbox --publish public
~~~

To publish from new uploads on a source channel, replace the example handle below with its YouTube handle, channel URL, or channel ID. The first scan records the current feed. Subsequent scans process new arrivals. Add `--include-latest` on the first scan to process the most recent existing video immediately. The worker recognizes its own recorded uploads when the source and destination are the same channel. [Find your channel ID](https://www.youtube.com/account_advanced)

~~~sh
python shorts.py watch "@YourChannel" --publish public
~~~

Leave the worker running. It checks every 60 seconds. The first spoken video downloads and caches a Whisper model. For a local preview, run `python shorts.py watch inbox`. The preview works before connecting YouTube. `Ctrl+C` stops the worker.

## What each Short contains

- A 1080 × 1920 H.264 video with AAC audio. The default layout keeps the full source picture over a blurred background. `--layout crop` fills the screen with a center crop.
- Captions embedded in the picture, with the spoken word highlighted. The worker also saves a matching UTF-8 `captions.srt`. To use your own subtitles, put `recording.srt` beside `recording.mp4` before moving the video into the inbox. Supplied subtitles use phrase-level captions.
- A title from the opening words. Highlight selection favors continuous speech, complete sentences, and natural pauses. The default is one highlight of up to 35 seconds per source. The worker can choose fewer than the requested count when there are too few distinct passages.

The output is under `shorts-output/clips/`. Each clip folder contains `short.mp4`, `captions.srt`, and `details.json` with its title and source time range. A successful upload prints its YouTube Shorts link.

## Controls

| Setting | Effect |
| --- | --- |
| `--clips 3 --seconds 45` | Select up to three nonoverlapping passages per source, up to 45 seconds each. Duration accepts 5–180 seconds. |
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

If YouTube accepts a final chunk and its response is lost, the worker asks the existing upload session for the result. If that session has expired, it pauses that clip. Find the title in YouTube Studio, then run one of the following commands using the generated clip folder. Use `--retry` only after confirming the clip is absent from Studio.

~~~sh
python shorts.py reconcile "shorts-output/clips/CLIP_FOLDER" --video-id EXISTING_ID
python shorts.py reconcile "shorts-output/clips/CLIP_FOLDER" --retry
~~~

The Google token is stored under your user configuration directory in `shorts-clipper/youtube.json`. Set `SHORTS_CLIPPER_TOKEN` to choose a different token file. Keep the token, client JSON, and upload session state private.

To run the regression checks, use `python -m unittest discover -s tests -v` in the activated environment.
