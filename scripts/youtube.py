"""Upload a generated clip to YouTube and schedule its publication."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def default_token_path():
    return Path.home() / ".gtavi-claudecode" / "youtube-oauth-token.json"


def load_upload_settings(env_file):
    settings = {}
    for line in Path(env_file).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        settings[key.strip()] = value
    return settings


def save_credentials(token_path, credentials):
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    if os.name != "nt":
        token_path.chmod(0o600)


def reauthorize_from_env(settings, scopes):
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config({
        "installed": {
            "client_id": settings["YOUTUBE_CLIENT_ID"],
            "client_secret": settings["YOUTUBE_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        },
    }, scopes)
    return flow.run_local_server(port=0, access_type="offline", prompt="consent")


def authorize(client_secrets=None, token_path=None, settings=None, reauthorize=False):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.exceptions import RefreshError
    except ImportError as exc:
        raise RuntimeError("YouTube upload dependencies are missing; install requirements.txt.") from exc

    settings = settings or {}
    token_path = Path(token_path) if token_path else default_token_path()
    env_auth_keys = ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")
    supplied_env_auth = [key in settings for key in env_auth_keys]
    if any(supplied_env_auth):
        if not all(supplied_env_auth):
            raise ValueError("The OAuth env file must define client ID, client secret, and refresh token.")
        if settings.get("YOUTUBE_UPLOAD_ENABLED", "").lower() != "true":
            raise ValueError("YOUTUBE_UPLOAD_ENABLED must be true in the OAuth env file.")
        if not reauthorize:
            if token_path.is_file():
                cached = Credentials.from_authorized_user_file(str(token_path), SCOPES)
                if cached.client_id == settings["YOUTUBE_CLIENT_ID"] and cached.has_scopes(SCOPES):
                    try:
                        if cached.expired and cached.refresh_token:
                            cached.refresh(Request())
                    except RefreshError:
                        cached = None
                    if cached and cached.valid:
                        return cached
            credentials = Credentials(
                token=None,
                refresh_token=settings["YOUTUBE_REFRESH_TOKEN"],
                token_uri="https://oauth2.googleapis.com/token",
                client_id=settings["YOUTUBE_CLIENT_ID"],
                client_secret=settings["YOUTUBE_CLIENT_SECRET"],
                scopes=SCOPES,
            )
            try:
                credentials.refresh(Request())
            except RefreshError as exc:
                raise RuntimeError(
                    "Google rejected the refresh token. Reauthorize with --reauthorize to continue."
                ) from exc
            return credentials

        credentials = reauthorize_from_env(settings, SCOPES)
        save_credentials(token_path, credentials)
        return credentials

    credentials = None
    if token_path.is_file():
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid or not credentials.has_scopes(SCOPES):
        if not client_secrets:
            raise ValueError("Provide a client-secrets JSON file or OAuth credentials in an env file.")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
        credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    save_credentials(token_path, credentials)
    return credentials


def video_metadata(clip, settings=None):
    clip = Path(clip)
    settings = settings or {}
    metadata_path = clip.with_suffix(".json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    title = metadata.get("source_title") or metadata.get("hook") or clip.stem
    if metadata.get("source_title"):
        title = f"{title} - {clip.stem.replace('_', ' ').title()}"
    description = []
    channel = metadata.get("source_channel")
    source_url = metadata.get("source_url")
    if channel:
        description.append(f"Original creator: {channel}")
    if source_url:
        description.append(f"Source: {source_url}")
    description.append("#GTA6 #Shorts")
    body = {
        "snippet": {
            "title": title[:100],
            "description": "\n".join(description),
            "tags": [tag.strip() for tag in settings.get("YOUTUBE_DEFAULT_TAGS", "GTA6,Shorts").split(",")
                     if tag.strip()],
            "categoryId": settings.get("YOUTUBE_CATEGORY_ID", "22"),
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": None,
        },
    }
    made_for_kids = settings.get("YOUTUBE_MADE_FOR_KIDS")
    if made_for_kids is not None:
        normalized = made_for_kids.lower()
        if normalized not in ("true", "false"):
            raise ValueError("YOUTUBE_MADE_FOR_KIDS must be true or false.")
        body["status"]["selfDeclaredMadeForKids"] = normalized == "true"
    return body


def upload_and_schedule(clip, credentials, publish_in_hours, settings=None):
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise RuntimeError("YouTube upload dependencies are missing; install requirements.txt.") from exc

    clip = Path(clip)
    if not clip.is_file():
        raise FileNotFoundError(f"Clip not found: {clip}")

    publish_at = datetime.now(timezone.utc) + timedelta(hours=publish_in_hours)
    settings = settings or {}
    configured_privacy = settings.get("YOUTUBE_PRIVACY_STATUS", "private").lower()
    if configured_privacy != "private":
        print("Scheduled videos must upload as private; YouTube will publish them at the scheduled time.")
    body = video_metadata(clip, settings)
    body["status"]["publishAt"] = publish_at.isoformat(timespec="seconds").replace("+00:00", "Z")
    youtube = build("youtube", "v3", credentials=credentials)
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(str(clip), chunksize=8 * 1024 * 1024, resumable=True),
    )
    response = None
    while response is None:
        progress, response = request.next_chunk()
        if progress:
            print(f"Uploading {clip.name}: {progress.progress() * 100:.0f}%")

    video_id = response.get("id")
    if not video_id:
        raise RuntimeError(f"YouTube upload returned no video ID for {clip}")
    print(f"Scheduled {clip.name} for {body['status']['publishAt']}: https://youtu.be/{video_id}")


def main():
    parser = argparse.ArgumentParser(description="Upload and schedule a generated YouTube clip")
    parser.add_argument("clips", nargs="+")
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument("--client-secrets")
    auth.add_argument("--env-file", help="OAuth credentials and upload settings in a .env file")
    parser.add_argument("--settings-env-file", help="optional .env file for video metadata settings")
    parser.add_argument("--publish-in-hours", type=float, default=1)
    parser.add_argument("--reauthorize", action="store_true",
                        help="open browser OAuth consent and save a new token outside the repository")
    args = parser.parse_args()
    if args.publish_in_hours < 1:
        parser.error("--publish-in-hours must be at least 1")
    settings_path = args.settings_env_file or args.env_file
    settings = load_upload_settings(settings_path) if settings_path else {}
    auth_settings = settings if args.env_file else {}
    credentials = authorize(
        Path(args.client_secrets) if args.client_secrets else None,
        settings=auth_settings,
        reauthorize=args.reauthorize,
    )
    for index, clip in enumerate(args.clips):
        upload_and_schedule(
            clip,
            credentials,
            publish_in_hours=args.publish_in_hours + index,
            settings=settings,
        )


if __name__ == "__main__":
    main()
