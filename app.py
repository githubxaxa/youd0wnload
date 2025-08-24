import os
import re
import uuid
from threading import Thread
from flask import Flask, render_template, request, jsonify, send_file, after_this_request
from flask_socketio import SocketIO, join_room
import yt_dlp
import imageio_ffmpeg

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

DOWNLOAD_DIR = os.path.join(os.getcwd(), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# token -> {"path": final_file_path, "name": download_name}
DOWNLOAD_MAP = {}

# Resolve an ffmpeg binary path (works on Render)
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

# Strip ANSI color codes from yt-dlp strings (so they render cleanly in browser)
ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
def strip_ansi(s: str | None) -> str:
    return ANSI_RE.sub('', s or '')


def sanitize_filename(name: str) -> str:
    """Turn a video title into a safe, short filename."""
    if not name:
        return "download"
    cleaned = []
    for ch in name:
        if ch.isalnum() or ch in {" ", "-"}:
            cleaned.append(ch)
        else:
            cleaned.append(" ")
    base = "-".join("".join(cleaned).split())
    return (base.strip("-") or "download")[:120]


def pick_thumbnail(info: dict) -> str | None:
    """Pick a robust thumbnail (direct -> largest -> ytimg fallback)."""
    def norm(u: str) -> str:
        if u.startswith("//"):
            return "https:" + u
        return u

    t = info.get("thumbnail")
    if t:
        return norm(t)

    thumbs = info.get("thumbnails") or []
    best_url, best_area = None, -1
    for th in thumbs:
        url = th.get("url")
        if not url:
            continue
        w = int(th.get("width") or 0)
        h = int(th.get("height") or 0)
        area = w * h
        if area > best_area:
            best_area, best_url = area, norm(url)

    if best_url:
        return best_url

    vid = info.get("id")
    if vid:
        return f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
    return None


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/get_info", methods=["POST"])
def get_info():
    """Return title + thumbnail prior to download."""
    data = request.get_json() or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    # Safer defaults for PaaS networks (IPv4 + web client + UA; no colors)
    base_opts = {
        "quiet": True,
        "skip_download": True,
        "color": "never",
        "force_ipv4": True,
        "ffmpeg_location": FFMPEG_PATH,  # harmless during info fetch
        "extractor_args": {
            # Force the standard web player client to avoid GVS/PO token quirks
            "youtube": {"player_client": ["web"]}
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        },
    }

    try:
        with yt_dlp.YoutubeDL(base_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            thumb = pick_thumbnail(info)
            print(f"[DEBUG] Title: {info.get('title')!r} | Thumb: {thumb!r}")
            return jsonify({"title": info.get("title"), "thumbnail": thumb})
    except Exception as e:
        # Print full error in logs to help diagnose
        print(f"[ERROR] get_info failed: {e}")
        return jsonify({"error": "Could not fetch video info"}), 500


@socketio.on("subscribe")
def handle_subscribe(data):
    """Client joins a unique room to receive progress events."""
    room = (data or {}).get("progress_id")
    if room:
        join_room(room)
        return {"ok": True}
    return {"ok": False, "error": "missing progress_id"}


def make_progress_hook(progress_id: str):
    """Build a yt-dlp progress hook that emits events to the room."""
    def hook(d):
        try:
            st = d.get("status")
            if st == "downloading":
                socketio.emit("progress", {
                    "status": "downloading",
                    "percent": strip_ansi((d.get("_percent_str") or "").strip()),
                    "speed": strip_ansi(d.get("_speed_str") or ""),
                    "eta": strip_ansi(d.get("_eta_str") or ""),
                    "downloaded": d.get("downloaded_bytes"),
                    "total": d.get("total_bytes") or d.get("total_bytes_estimate"),
                }, to=progress_id)
            elif st == "finished":
                socketio.emit("progress", {"status": "finished"}, to=progress_id)
        except Exception:
            pass
    return hook


def run_download(url, option, progress_id):
    """Run yt-dlp in background, then provide a token-based link when ready."""
    try:
        ext = "mp4" if option == "1" else "mp3"
        unique_id = uuid.uuid4().hex[:6]  # ensure filesystem uniqueness
        outtmpl = os.path.join(DOWNLOAD_DIR, f"%(title)s-{unique_id}.%(ext)s")

        ydl_opts = {
            "outtmpl": outtmpl,
            "merge_output_format": "mp4" if option == "1" else None,
            "progress_hooks": [make_progress_hook(progress_id)],
            "quiet": False,
            "verbose": True,
            "overwrites": True,
            "color": "never",
            "force_ipv4": True,
            "ffmpeg_location": FFMPEG_PATH,
            "extractor_args": {
                "youtube": {"player_client": ["web"]}
            },
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                )
            },
        }

        if option == "1":
            # Video: best streams + convert to MP4 (H.264 + AAC)
            ydl_opts.update({
                "format": "bestvideo+bestaudio",
                "postprocessors": [
                    {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"},
                ],
                "postprocessor_args": [
                    "-c:v", "libx264",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-movflags", "faststart",
                    "-pix_fmt", "yuv420p",
                ],
            })
        else:
            # Audio: MP3
            ydl_opts.update({
                "format": "bestaudio/best",
                "postprocessors": [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                ],
            })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            # Resolve final path after postprocessing
            final_path = ydl.prepare_filename(info)
            if option == "2":
                final_path = os.path.splitext(final_path)[0] + ".mp3"
            else:
                final_path = os.path.splitext(final_path)[0] + ".mp4"

            # Verify final file exists and non-empty
            if not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
                socketio.emit("progress", {"status": "error", "message": "The downloaded file is empty"}, to=progress_id)
                return

            # Friendly name for browser download prompt
            title = sanitize_filename(info.get("title", "download"))
            download_name = f"{title}.{ext}"

        # Map a one-time token to this file
        token = uuid.uuid4().hex
        DOWNLOAD_MAP[token] = {"path": final_path, "name": download_name}

        # Notify the client it's ready
        socketio.emit("progress", {
            "status": "ready",
            "url": f"/download/{token}",
            "filename": download_name
        }, to=progress_id)

    except Exception as e:
        print(f"[ERROR] run_download failed: {e}")
        socketio.emit("progress", {"status": "error", "message": str(e)}, to=progress_id)


@app.route("/start_download", methods=["POST"])
def start_download():
    url = (request.form.get("url") or "").strip()
    option = (request.form.get("option") or "").strip()
    progress_id = (request.form.get("progress_id") or "").strip()

    if not url or option not in {"1", "2"} or not progress_id:
        return "Missing or invalid parameters", 400

    Thread(target=run_download, args=(url, option, progress_id)).start()
    return jsonify({"started": True})


@app.route("/download/<token>")
def download_file(token):
    info = DOWNLOAD_MAP.pop(token, None)
    if not info:
        return "Link expired or invalid", 404

    file_path, download_name = info["path"], info["name"]
    if not os.path.exists(file_path):
        return "File not found", 404

    @after_this_request
    def cleanup(resp):
        try:
            os.remove(file_path)
        except Exception as e:
            print(f"[WARN] Cleanup failed: {e}")
        return resp

    return send_file(file_path, as_attachment=True, download_name=download_name)


if __name__ == "__main__":
    socketio.run(app, debug=True, use_reloader=False)
