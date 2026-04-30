#!/usr/bin/env python3
import sys
import signal
import urllib.parse
import threading
import shutil
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from feedgen.feed import FeedGenerator

# Configuration
MEDIA_BASE_DIR = Path("/Users/jeff/Music/Podcasts")
HOST_NAME = "localhost"
PORT = 8080

class PodcastHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        decoded_path = urllib.parse.unquote(self.path).strip("/")
        parts = [p for p in decoded_path.split("/") if p]

        try:
            if len(parts) == 2:
                self.generate_rss(parts[0], parts[1])
            elif len(parts) == 3:
                self.serve_file(parts[0], parts[1], parts[2])
            else:
                self.send_error(404, "Invalid Path.")
        except Exception as e:
            self.send_error(500, f"Internal Server Error: {e}")

    def generate_rss(self, artist: str, album: str):
        album_path = MEDIA_BASE_DIR / artist / album
        if not album_path.is_dir():
            self.send_error(404, "Course directory not found.")
            return

        # 1. Load Local Configuration (podcaster.toml)
        config = {}
        config_path = album_path / "podcaster.toml"
        if config_path.exists():
            with open(config_path, "rb") as f:
                config = tomllib.load(f)

        # 2. Extract Metadata with Defaults
        title = config.get("title", f"{artist}: {album}")
        description = config.get("description", f"Lectures for {album}")

        # Anchoring the date (Default to 2026-01-01 if not provided)
        start_date_str = config.get("start_date", "2026-01-01")
        current_date = datetime.fromisoformat(start_date_str).replace(tzinfo=timezone.utc)

        # 3. Look for supplementary PDF
        pdf_file = next(album_path.glob("*.pdf"), None)
        if pdf_file:
            pdf_url = f"http://{HOST_NAME}:{PORT}/{artist}/{album}/{urllib.parse.quote(pdf_file.name)}"
            description += f'\n\nCourse Handouts (PDF): <a href="{pdf_url}">Download Here</a>'

        fg = FeedGenerator()
        fg.load_extension('podcast')
        fg.title(title)
        fg.description(description)
        fg.link(href=f"http://{HOST_NAME}:{PORT}/{artist}/{album}", rel='alternate')

        # 4. Generate Episodes
        for mp3 in sorted(album_path.glob("*.mp3")):
            fe = fg.add_entry()
            fe.id(mp3.name)
            fe.title(mp3.stem)

            # Use anchored date, incrementing by 1 day per lecture
            fe.pubDate(current_date)
            current_date += timedelta(days=1)

            safe_name = urllib.parse.quote(mp3.name)
            file_url = f"http://{HOST_NAME}:{PORT}/{artist}/{album}/{safe_name}"
            fe.enclosure(file_url, str(mp3.stat().st_size), 'audio/mpeg')

        response = fg.rss_str(pretty=True)
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.end_headers()
        self.wfile.write(response)

    def serve_file(self, artist, album, filename):
        file_path = MEDIA_BASE_DIR / artist / album / filename
        if not file_path.exists():
            self.send_error(404)
            return

        # Handle Content-Type dynamically for MP3 vs PDF
        content_type = "audio/mpeg" if file_path.suffix == ".mp3" else "application/pdf"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_path.stat().st_size))
        self.end_headers()

        with open(file_path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

class PodcasterService:
    """Manages the lifecycle of the ThreadingHTTPServer."""
    def __init__(self):
        self.server = ThreadingHTTPServer((HOST_NAME, PORT), PodcastHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=False)
        self.exit_event = threading.Event()

    def start(self):
        print(f"Podcaster listening on http://{HOST_NAME}:{PORT}")
        self.server_thread.start()

    def stop(self, signum=None, frame=None):
        """Handles graceful shutdown on SIGINT/SIGTERM."""
        sig = signal.Signals(signum).name if signum else "Manual"
        print(f"\n[{sig}] Initiating graceful shutdown...")
        self.server.shutdown() # Stops the serve_forever loop cleanly
        self.server.server_close()
        self.server_thread.join() # Wait for active requests to finish
        self.exit_event.set()

def main():
    service = PodcasterService()

    # Register OS signals for clean termination
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, service.stop)

    service.start()

    # Main thread parks here until shutdown is triggered
    service.exit_event.wait()
    print("Process exited cleanly.")

if __name__ == "__main__":
    main()
