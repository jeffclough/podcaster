#!/usr/bin/env python3
import sys
import signal
import urllib.parse
import threading
import shutil
import tomllib
import socket
import eyed3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from feedgen.feed import FeedGenerator

# Configuration
MEDIA_BASE_DIR = Path("~/Music/Podcasts").expanduser()
PORT = 8080

def get_local_ip():
    """
    Retrieves the local IP address of the machine.
    Works by triggering the OS routing table without sending data.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't even have to be reachable
        s.connect(('8.8.8.8', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

LOCAL_IP = get_local_ip()

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

    def generate_rss(self, creator: str, podcast: str):
        podcast_path = MEDIA_BASE_DIR / creator / podcast
        if not podcast_path.is_dir():
            self.send_error(404, f"Course directory '{podcast_path}' not found.")
            return

        # 1. Load Local Configuration (podcaster.toml)
        config = {}
        config_path = podcast_path / "podcaster.toml"
        if config_path.exists():
            with open(config_path, "rb") as f:
                config = tomllib.load(f)

        # 2. Extract Metadata with Defaults
        group=config.get('podcast',{})
        title = group.get("title", f"{creator}: {podcast}")
        description = group.get("description", "")

        # Anchoring the date (Default to Noon on 2026-01-01 if not provided)
        group=config.get('episodes',{})
        start_time_str = group.get("start_time", "2026-01-01T12:00:00")
        time_interval = group.get("time_interval", dict(days=1))
        start_time = datetime.fromisoformat(start_time_str).replace(tzinfo=timezone.utc)
        #time_interval = eval(group.get("time_interval", "timedelta(days=1)"))
        time_interval = timedelta(**time_interval)
        use_recording_date = group.get("use_recording_date",False)
        
        # 3. Look for supplementary PDF
        pdf_file = next(podcast_path.glob("*.pdf"), None)
        if pdf_file:
            pdf_url = f"http://{LOCAL_IP}:{PORT}/{creator}/{podcast}/{urllib.parse.quote(pdf_file.name)}"
            description += f'\n\nCourse Handouts (PDF): <a href="{pdf_url}">Download Here</a>'

        fg = FeedGenerator()
        fg.load_extension('podcast')
        fg.title(title)
        fg.description(description)
        fg.link(href=f"http://{LOCAL_IP}:{PORT}/{creator}/{podcast}", rel='alternate')

        # 4. Generate Episodes
        # Set up to supply our own publication date values (just in case).
        pub_date=start_time
        for mp3 in sorted(podcast_path.glob("*.mp3")):
            fe = fg.add_entry()
            fe.id(mp3.name)
            fe.title(mp3.stem)

            # Either get the publication date from the MP3 file or use
            # the pub_date value we're maintaining in this loop. In
            # either case, set this episode's publication date
            # accordingly.
            if use_recording_date:
                afile = eyed3.load(mp3)
                if afile:
                    d = afile.tag.getBestDate()
                    pub_date = datetime(
                        year=d.year, month=d.month, day=d.day,
                        hour=d.hour or 0,minute=d.minute or 0,second=d.second or 0,
                        tzinfo=timezone.utc
                    )
            fe.pubDate(pub_date)
            pub_date += time_interval

            safe_name = urllib.parse.quote(mp3.name)
            file_url = f"http://{LOCAL_IP}:{PORT}/{creator}/{podcast}/{safe_name}"
            fe.enclosure(file_url, str(mp3.stat().st_size), 'audio/mpeg')

        response = fg.rss_str(pretty=True)
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.end_headers()
        self.wfile.write(response)

    def serve_file(self, creator, podcast, filename):
        file_path = MEDIA_BASE_DIR / creator / podcast / filename
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
        self.server = ThreadingHTTPServer(('', PORT), PodcastHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=False)
        self.exit_event = threading.Event()

    def start(self):
        print(f"Podcaster listening on http://{LOCAL_IP}:{PORT}")
        # List the available podcast directories.
        print("Available Podcast URLs:")
        for podcast_path in sorted(MEDIA_BASE_DIR.glob("*/*/")):
            # Make sure we have both a creator and a podcast name.
            parts = podcast_path.relative_to(MEDIA_BASE_DIR).parts
            if len(parts)!=2:
                continue
            creator,podcast_name=parts
            # Make sure we ignore hidden directories.
            if creator.startswith('.') or podcast_name.startswith('.'):
                continue
            # Print the URL-quoted URL we've found.
            path=urllib.parse.quote(f"{creator}/{podcast_name}")
            print(f"  http://{LOCAL_IP}:{PORT}/{path}")
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
