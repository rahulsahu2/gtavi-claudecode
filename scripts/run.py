"""One-command GTA 6 clipping workflow.

Find a trending video (or use --source), detect moments, cut vertical clips,
and run the pre-post checker. YouTube uploads are opt-in.

usage: python scripts/run.py [--source URL-or-file] [--clips 3]
                             [--layout blur] [--query "gta 6"] [--youtube-upload]
"""
import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def command(args):
    print("+", " ".join(str(a) for a in args), flush=True)
    return subprocess.run([str(a) for a in args], check=False)


def has_ffmpeg():
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def missing_python_packages(youtube_upload=False):
    packages = ["faster_whisper", "numpy", "requests", "yt_dlp",
                "deep_translator", "edge_tts", "cv2"]
    if youtube_upload:
        packages.extend(["googleapiclient", "google_auth_oauthlib"])
    return [name for name in packages if importlib.util.find_spec(name) is None]


def ensure_setup(root, install, youtube_upload=False):
    missing = missing_python_packages(youtube_upload)
    ffmpeg_missing = not has_ffmpeg()
    if not install:
        problems = []
        if missing:
            problems.append("Python packages: " + ", ".join(missing))
        if ffmpeg_missing:
            problems.append("ffmpeg/ffprobe")
        if problems:
            sys.exit("Missing setup dependencies: " + "; ".join(problems) +
                     "\nRun this command again with --install, or install them manually.")
        return

    if missing:
        result = command([sys.executable, "-m", "pip", "install", "-U", "-r", str(root / "requirements.txt")])
        if result.returncode:
            sys.exit("Python dependency installation failed.")

    if not has_ffmpeg():
        if sys.platform == "win32":
            result = command(["winget", "install", "--id", "Gyan.FFmpeg", "-e", "--source", "winget"])
        elif sys.platform == "darwin":
            result = command(["brew", "install", "ffmpeg"])
        else:
            result = command(["sudo", "apt", "install", "-y", "ffmpeg"])
        if result.returncode:
            sys.exit("ffmpeg installation failed.")


def top_trend(root, query, hours, limit):
    out = root / "trends.md"
    result = command([sys.executable, str(root / "scripts" / "trends.py"),
                      "--query", query, "--hours", str(hours), "--limit", str(limit),
                      "--out", str(out)])
    if result.returncode:
        sys.exit("Trend discovery failed.")
    text = out.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"\]\((https://(?:www\.)?(?:youtube\.com|youtu\.be)/[^)]+)\)", text)
    if not match:
        sys.exit("No trending video was found. Pass --source URL-or-file to run on a specific video.")
    return match.group(1)


def candidate_count(job):
    candidates = job / "candidates.json"
    if not candidates.exists():
        return 0
    import json
    data = json.loads(candidates.read_text(encoding="utf-8"))
    return len(data.get("candidates", []))


def main():
    ap = argparse.ArgumentParser(description="Run the GTA 6 clip workflow end-to-end")
    ap.add_argument("--source", help="YouTube URL or local video; omitted = top trending video")
    ap.add_argument("--clips", type=int, default=3, help="number of clips to create")
    ap.add_argument("--layout", choices=["fill", "blur", "facecam", "react"], default="blur")
    ap.add_argument("--query", default="gta 6")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--hook", default="Wait for this", help="hook shown in the first seconds")
    ap.add_argument("--install", action="store_true", help="install missing Python packages and ffmpeg")
    ap.add_argument("--campaign", default="", help="optional campaign rules file")
    ap.add_argument("--youtube-upload", action="store_true",
                    help="upload passing clips to YouTube and schedule one per hour (requires OAuth)")
    ap.add_argument("--youtube-client-secrets", default=os.environ.get("YOUTUBE_CLIENT_SECRETS", ""),
                    help="Google OAuth Desktop app client-secrets JSON (or set YOUTUBE_CLIENT_SECRETS)")
    ap.add_argument("--youtube-env-file", help="OAuth credentials and upload settings in a .env file")
    a = ap.parse_args()
    if a.clips < 1 or a.clips > 20:
        ap.error("--clips must be between 1 and 20")
    if a.youtube_upload and bool(a.youtube_client_secrets) == bool(a.youtube_env_file):
        ap.error("--youtube-upload requires exactly one of --youtube-client-secrets or --youtube-env-file")
    if a.youtube_upload and a.youtube_client_secrets and not Path(a.youtube_client_secrets).is_file():
        ap.error(f"YouTube client-secrets file not found: {a.youtube_client_secrets}")
    if a.youtube_upload and a.youtube_env_file and not Path(a.youtube_env_file).is_file():
        ap.error(f"YouTube env file not found: {a.youtube_env_file}")

    root = Path(__file__).resolve().parent.parent
    os.environ.setdefault("CLIP_DESK_HOME", str(root))
    sys.path.insert(0, str(root / "scripts"))
    from common import job_dir
    upload_settings = {}
    if a.youtube_upload and a.youtube_env_file:
        from youtube import load_upload_settings
        upload_settings = load_upload_settings(a.youtube_env_file)
        if upload_settings.get("YOUTUBE_UPLOAD_ENABLED", "").lower() != "true":
            ap.error("YOUTUBE_UPLOAD_ENABLED must be true in the YouTube env file")
        for key in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"):
            if not upload_settings.get(key):
                ap.error(f"YouTube env file is missing {key}")

    ensure_setup(root, a.install, a.youtube_upload)

    source = a.source or top_trend(root, a.query, a.hours, a.limit)
    print(f"Source: {source}")

    moments = command([sys.executable, str(root / "scripts" / "moments.py"), source,
                       "--top", str(a.clips), "--max-minutes", "90"])
    if moments.returncode:
        sys.exit("Moment detection failed.")

    job = job_dir(source)
    if not (job / "candidates.json").exists():
        sys.exit("Moment detection did not create a candidates file.")

    count = min(a.clips, candidate_count(job))
    if not count:
        sys.exit("No clip candidates were found.")

    created = []
    for pick in range(1, count + 1):
        cut = command([sys.executable, str(root / "scripts" / "cut.py"), str(job),
                       "--pick", str(pick), "--layout", a.layout, "--hook", a.hook])
        if cut.returncode:
            print(f"Clip {pick} could not be cut; continuing.", file=sys.stderr)
            continue
        clip = job / "edits" / f"clip_{pick}.mp4"
        if clip.exists():
            created.append(clip)

    if not created:
        sys.exit("No clips were created.")

    campaign = a.campaign or (root / "campaigns.md")
    passing_clips = []
    for clip in created:
        args = [sys.executable, str(root / "scripts" / "check.py"), str(clip)]
        if Path(campaign).exists():
            args += ["--campaign", str(campaign)]
        check = command(args)
        if check.returncode:
            print(f"Review required for {clip}", file=sys.stderr)
        else:
            passing_clips.append(clip)

    if a.youtube_upload and passing_clips:
        from youtube import authorize, upload_and_schedule
        credentials = authorize(
            Path(a.youtube_client_secrets) if a.youtube_client_secrets else None,
            settings=upload_settings,
        )
        for index, clip in enumerate(passing_clips):
            upload_and_schedule(
                clip,
                credentials,
                publish_in_hours=index + 1,
                settings=upload_settings,
            )

    print("\nWorkflow complete.")
    print("Created clips:")
    for clip in created:
        print(f"  {clip}")
    if a.youtube_upload and passing_clips:
        print("Passing clips were uploaded and scheduled on YouTube.")
    elif a.youtube_upload:
        print("No clips passed the pre-post check; nothing was uploaded.")
    else:
        print("Nothing was uploaded. Use --youtube-upload to opt into YouTube scheduling.")


if __name__ == "__main__":
    main()
