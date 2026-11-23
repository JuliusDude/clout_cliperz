"""Google OAuth and durable YouTube Data API resumable uploads."""

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from .media import VIDEO_ID
from .storage import ClipperError, existing_thumbnail, file_hash, read_json, save_json
from .workflows import CLOUT_CUTS, CLOUT_HANDLE, MOVIE_RECAP, WORKFLOWS


SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
INSERT = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEO_STATUS = "https://www.googleapis.com/youtube/v3/videos"
SET_THUMBNAIL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
CHUNK_SIZE = 8 * 1024 * 1024  # YouTube requires multiples of 256 KiB.
TRANSIENT = {408, 429, 500, 502, 503, 504}


class RetryUpload(Exception):
    pass


def token_path(workflow=CLOUT_CUTS):
    recap = workflow == MOVIE_RECAP
    override = os.environ.get("SHORTS_CLIPPER_MOVIE_RECAP_TOKEN" if recap else "SHORTS_CLIPPER_TOKEN")
    if override:
        return Path(override).expanduser().resolve()
    base = os.environ.get("APPDATA" if os.name == "nt" else "XDG_CONFIG_HOME")
    filename = "youtube-movie-recap.json" if recap else "youtube.json"
    return (Path(base) if base else Path.home() / ".config") / "shorts-clipper" / filename


def connect(client_file, workflow=CLOUT_CUTS):
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as error:
        raise ClipperError("Install requirements.txt before connecting YouTube.") from error
    client = read_json(client_file)
    if not client or "installed" not in client:
        raise ClipperError("Choose the OAuth client JSON for a Google Desktop app.")
    flow = InstalledAppFlow.from_client_config(client, SCOPES)
    credentials = flow.run_local_server(
        host="127.0.0.1", port=0, access_type="offline", prompt="consent",
        authorization_prompt_message=f"Open this link to choose the {WORKFLOWS[workflow]} destination channel:\n{{url}}",
        success_message="YouTube authorization received. You can return to Shorts Clipper.",
    )
    if not credentials.refresh_token:
        raise ClipperError("Google did not grant offline access. Run connect again to authorize the worker.")
    if credentials.granted_scopes and not set(SCOPES).issubset(credentials.granted_scopes):
        raise ClipperError("Grant both upload and read access to check processing and apply thumbnails.")
    if workflow != MOVIE_RECAP:
        from google.auth.transport.requests import AuthorizedSession
        with AuthorizedSession(credentials) as client:
            verify_clout_channel(client)
    save_json(token_path(workflow), json.loads(credentials.to_json()))
    print(f"YouTube connected for {WORKFLOWS[workflow]}. The worker can now upload to the channel you selected.")


def verify_clout_channel(client):
    endpoint = "https://www.googleapis.com/youtube/v3/channels"

    def channel_ids(filters):
        response = client.get(endpoint, params={"part": "id", **filters}, timeout=30)
        if response.status_code != 200:
            raise api_error(response)
        return [item["id"] for item in response.json().get("items", [])]

    expected = channel_ids({"forHandle": CLOUT_HANDLE})
    if len(expected) != 1 or channel_ids({"mine": "true"}) != expected:
        raise ClipperError(f"Choose {CLOUT_HANDLE} in Google authorization before uploading.")


def connection_command(workflow):
    suffix = "" if workflow == CLOUT_CUTS else f" --workflow {workflow}"
    return f"python shorts.py connect client_secret.json{suffix}"


def session(workflow=CLOUT_CUTS):
    data = read_json(token_path(workflow))
    if not data:
        raise ClipperError(f"Connect YouTube for {WORKFLOWS[workflow]} first: {connection_command(workflow)}")
    if not set(SCOPES).issubset(data.get("scopes", [])):
        raise ClipperError(f"Reconnect with {connection_command(workflow)} to enable thumbnail processing checks.")
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import AuthorizedSession
    except ImportError as error:
        raise ClipperError("Install requirements.txt before publishing.") from error
    client = AuthorizedSession(Credentials.from_authorized_user_info(data, SCOPES))
    try:
        if workflow != MOVIE_RECAP:
            verify_clout_channel(client)
        return client
    except Exception:
        client.close()
        raise


def api_error(response):
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = response.reason or "Request failed"
    return ClipperError(f"YouTube returned HTTP {response.status_code}: {str(message)[:450]}")


def check_url(url):
    parsed = urlsplit(url or "")
    if parsed.scheme != "https" or parsed.hostname not in {
        "www.googleapis.com", "youtube.googleapis.com", "upload.youtube.com",
        "upload.video.google.com", "content.googleapis.com",
    }:
        raise ClipperError("YouTube returned an unexpected resumable upload address.")
    return url


def initiate(client, details, privacy, audience, size):
    status = {"privacyStatus": privacy}
    if audience:
        status["selfDeclaredMadeForKids"] = audience == "kids"
    snippet = {"title": details["title"], "description": details["description"], "categoryId": "22"}
    response = client.post(
        INSERT, params={"uploadType": "resumable", "part": "snippet,status"},
        headers={"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
        json={"snippet": snippet, "status": status}, timeout=(15, 60), allow_redirects=False,
    )
    if response.status_code in TRANSIENT:
        raise RetryUpload()
    if response.status_code not in {200, 201}:
        raise api_error(response)
    return check_url(response.headers.get("Location"))


def offset(response, size):
    value = response.headers.get("Range")
    if not value:
        return 0
    match = re.fullmatch(r"bytes=0-(\d+)", value)
    if not match or int(match[1]) >= size:
        raise ClipperError("YouTube returned an unexpected upload offset. Retry with the same output folder.")
    return int(match[1]) + 1


def publish(client, folder, details, state, privacy, audience):
    import requests

    receipt = state / "uploads" / (details["upload_key"] + ".json")
    record = read_json(receipt, {})
    if record.get("video_id"):
        print("Already uploaded: https://www.youtube.com/shorts/" + record["video_id"], flush=True)
        return record["video_id"]
    video = folder / "short.mp4"
    size = video.stat().st_size
    if not size:
        raise ClipperError(f"The rendered video is empty: {video}")
    fingerprint = file_hash(video)
    if record and record.get("fingerprint") != fingerprint:
        for original in folder.parent.glob(details["upload_key"][:12] + "-*/short.mp4"):
            if original != video and file_hash(original) == record.get("fingerprint"):
                video, size = original, original.stat().st_size
                break
        else:
            raise ClipperError("The unfinished upload needs its original MP4. Restore the original render to resume.")
    position = None
    failures = 0
    with video.open("rb") as stream:
        while True:
            try:
                if not record:
                    url = initiate(client, details, privacy, audience, size)
                    record = {"session": url, "fingerprint": fingerprint, "final_sent": False,
                              "requested_privacy": privacy, "title": details["title"]}
                    save_json(receipt, record)
                    position = 0
                url = check_url(record["session"])
                if position is None or position == size:
                    response = client.put(url, headers={"Content-Range": f"bytes */{size}", "Content-Length": "0"},
                                          data=b"", allow_redirects=False, timeout=(15, 60))
                else:
                    stream.seek(position)
                    chunk = stream.read(CHUNK_SIZE)
                    last = position + len(chunk) - 1
                    if last == size - 1 and not record["final_sent"]:
                        record["final_sent"] = True
                        save_json(receipt, record)
                    headers = {"Content-Type": "video/mp4", "Content-Length": str(len(chunk)),
                               "Content-Range": f"bytes {position}-{last}/{size}"}
                    response = client.put(url, headers=headers, data=chunk,
                                          allow_redirects=False, timeout=(15, 180))
                code = response.status_code
                if code in {200, 201}:
                    result = response.json()
                    ident = result.get("id", "")
                    if not VIDEO_ID.fullmatch(ident):
                        raise ClipperError("YouTube gave no video ID. Retry to check this same upload session.")
                    final = {"video_id": ident, "title": record["title"],
                             "requested_privacy": record["requested_privacy"],
                             "privacy": result.get("status", {}).get("privacyStatus")}
                    save_json(receipt, final)
                    print(f"Uploaded ({final['privacy'] or record['requested_privacy']}): "
                          f"https://www.youtube.com/shorts/{ident}", flush=True)
                    return ident
                if code in {404, 410}:
                    if record["final_sent"]:
                        raise ClipperError(f"The final upload receipt expired. Check YouTube Studio for "
                                           f"'{record['title']}' before reconciling {receipt}. See README.")
                    receipt.unlink()
                    record, position = {}, None
                elif code == 308:
                    acknowledged = offset(response, size)
                    if position is None or acknowledged > position:
                        if position is not None:
                            failures = 0
                        position = acknowledged
                        continue
                    position = None
                elif code not in TRANSIENT:
                    raise api_error(response)
                else:
                    position = None
            except (RetryUpload, requests.Timeout, requests.ConnectionError):
                # The last PUT may have succeeded. Query its existing session.
                position = None
            failures += 1
            if failures >= 5:
                raise ClipperError("Upload interrupted. Retry with the same output folder to resume.")
            time.sleep(min(16, 2 ** (failures - 1)))


def publish_with_thumbnail(client, folder, details, state, privacy, audience):
    import requests

    thumbnail = existing_thumbnail(folder)
    checksum = file_hash(thumbnail)
    ident = publish(client, folder, details, state, privacy, audience)
    receipt = state / "uploads" / (details["upload_key"] + ".json")
    record = read_json(receipt)
    if record.get("thumbnail_sha256") == checksum:
        return ident

    params = {"part": "processingDetails,status", "id": ident,
              "fields": "items(processingDetails(processingStatus,processingFailureReason),status(uploadStatus))"}
    processed = False
    for delay in (0, 5, 10, 20, 30, 45, 60):
        if delay:
            time.sleep(delay)
        try:
            if not processed:
                response = client.get(VIDEO_STATUS, params=params, timeout=(15, 45))
                if response.status_code in TRANSIENT:
                    continue
                if response.status_code != 200:
                    raise api_error(response)
                items = response.json().get("items", [])
                item = items[0] if items else {}
                progress = item.get("processingDetails", {})
                status = progress.get("processingStatus")
                if status in {"failed", "terminated"} or item.get("status", {}).get("uploadStatus") in {"failed", "rejected", "deleted"}:
                    reason = progress.get("processingFailureReason") or item.get("status", {}).get("uploadStatus")
                    raise ClipperError(f"YouTube could not process video {ident} ({reason or status}). Check Studio.")
                processed = status == "succeeded"
                if not processed:
                    continue
            with thumbnail.open("rb") as content:
                response = client.post(SET_THUMBNAIL, params={"videoId": ident, "uploadType": "media"},
                                       headers={"Content-Type": "image/jpeg"}, data=content, timeout=(15, 90))
            if response.status_code in TRANSIENT or response.status_code == 404:
                continue
            if response.status_code != 200:
                raise api_error(response)
        except (requests.Timeout, requests.ConnectionError):
            continue
        record["thumbnail_sha256"] = checksum
        save_json(receipt, record)
        print(f"Thumbnail applied: https://www.youtube.com/shorts/{ident}", flush=True)
        return ident
    raise ClipperError(f"Video {ident} is uploaded. Its thumbnail is pending. Rerun the same job to finish.")


def reconcile(folder, existing_id):
    from .storage import work_lock

    folder = folder.expanduser().resolve()
    details = read_json(folder / "details.json", {})
    key = details.get("upload_key", "")
    if folder.parent.name != "clips" or not re.fullmatch(r"[0-9a-f]{32}", key):
        raise ClipperError("Choose a generated clip folder containing details.json.")
    if existing_id is not None and not VIDEO_ID.fullmatch(existing_id):
        raise ClipperError("Supply the 11-character YouTube video ID from Studio.")
    state = folder.parent.parent / ".state"
    receipt = state / "uploads" / (key + ".json")
    with work_lock(state):
        record = read_json(receipt, {})
        if record.get("video_id") or not record.get("final_sent"):
            raise ClipperError("This clip has no uncertain final upload to reconcile.")
        if existing_id:
            save_json(receipt, {"video_id": existing_id, "title": record["title"],
                                "requested_privacy": record["requested_privacy"], "privacy": None})
            print("Recorded the existing upload: https://www.youtube.com/shorts/" + existing_id)
        else:
            receipt.unlink()
            print("Cleared the unfinished receipt. The next publishing run will retry the clip.")
