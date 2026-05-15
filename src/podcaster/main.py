#!/usr/bin/env python3
import eyed3
import shutil
import signal
import socket
import sys
import threading
import tomllib
import traceback
import urllib.parse
from argparse import ArgumentParser
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from debug import DebugChannel
from feedgen.feed import FeedGenerator
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Configuration
MEDIA_BASE_DIR = Path("~/Music/Podcasts").expanduser()
PORT = 8080

# Create a DebugChannel.
dc = DebugChannel(False)

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
        try:
            podcast_path = Path(urllib.parse.unquote(str(MEDIA_BASE_DIR/creator/podcast)))
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
            description = group.get("description", f"Podcast: {podcast}\nFrom: {creator}")

            # Anchoring the date (Default to Noon on 2026-01-01 if not provided)
            group=config.get('episodes',{})
            start_time_str = group.get("start_time", "2026-01-01T12:00:00")
            time_interval = group.get("time_interval", dict(days=1))
            start_time = datetime.fromisoformat(start_time_str).replace(tzinfo=timezone.utc)
            #time_interval = eval(group.get("time_interval", "timedelta(days=1)"))
            time_interval = timedelta(**time_interval)
            dc(f"start_time={start_time.isoformat()}")
            dc(f"{time_interval=}")
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
            if False:
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
                            dc(f"{pub_date}={pub_daate.isoformat()}")
                    fe.pubDate(pub_date)
                    pub_date += time_interval

                    safe_name = urllib.parse.quote(mp3.name)
                    file_url = f"http://{LOCAL_IP}:{PORT}/{creator}/{podcast}/{safe_name}"
                    fe.enclosure(file_url, str(mp3.stat().st_size), 'audio/mpeg')

            else:
                pub_date=start_time
                episodes=[Episode(fn) for fn in sorted(podcast_path.glob("*.mp3"))]
                for ep in episodes:
                    fe=fg.add_entry()
                    if use_recording_date:
                        pub_date=ep.datetime
                    fe.id(ep.path.stem)
                    fe.title(ep.title)
                    dc(f"ep.datetime={ep.datetime.isoformat()}")
                    fe.pubDate(ep.datetime)
                    quoted_path=urllib.parse.quote(str(ep.path))
                    fe.enclosure(f"http://{LOCAL_IP}:{PORT}/{quoted_path}",str(ep.size),'audio/mpeg')
                    fe.pubDate(pub_date)
                    pub_date += time_interval

            response = fg.rss_str(pretty=True)
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.end_headers()
            self.wfile.write(response)
        except Exception as e:
            print(traceback.format_exc())
            raise

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

def run_service():
    service = PodcasterService()

    # Register OS signals for clean termination
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, service.stop)

    service.start()

    # Main thread parks here until shutdown is triggered
    service.exit_event.wait()
    print("Process exited cleanly.")

def say(msg,rc=None):
    print(f"{Path(sys.argv[0]).stem}: {msg}")
    if rc is not None:
        sys.exit(rc)

class Episode():
    def __init__(self,path):
        # Make sure the MP3 filename is a proper Path instance.
        if not isinstance(path,Path):
            path=Path(path)
        self.path=path

        # Load the MP3 metadata.
        self.mp3=eyed3.load(self.path)
        if not self.mp3:
            raise ValueError(f"Can't load ID3 data from {self.path}.")
        if self.mp3.tag is None:
            self.mp3.initTag()
        tag=self.mp3.tag # Just to make the code below simpler.

        # Initialize our properties and state data for this episode.
        # Be sure the datetime we get has no fractional seconds.
        dt=tag.getBestDate()
        dt=datetime(
            year=dt.year,month=dt.month,day=dt.day,
            hour=dt.hour or 0,minute=dt.minute or 0,second=int(dt.second or 0)
        ).replace(tzinfo=timezone.utc)
        dc(f"dt={dt.isoformat()}")
        for image in tag.images:
            if image.picture_type==ImageFrame.FRONT_COVER:
                image=image.image_data
                break
        else:
            image=None
        self._prop=dict(
            creator=tag.artist,
            podcast=tag.album,
            episode=tag.track_num,
            datetime=dt,
            title=tag.title,
            cover=image,
            size=self.mp3.info.size_bytes
        )
        self._is_dirty=dict(
            creator=False,
            podcast=False,
            episode=False,
            datetime=False,
            title=False,
            cover=False,
        )
        self._in_context=False

    def save(self):
        """
        Save any new tag data back to the MP3 file if there have been
        any updates.
        """

        # Check for any dirty (i.e. changed) properties.
        if any(self._is_dirty.values()):
            # Update our MP3 tag data.
            tag=self.mp3.tag
            if self._is_dirty['creator']:
                tag.artist=self._prop['creator']
            if self._is_dirty['podcast']:
                tag.album=self._prop['podcast']
            if self._is_dirty['episode']:
                tag.track_num=self._prop['episode']
            if self._is_dirty['datetime']:
                # Convert our datetime to ISO format.
                tag.recording_date=self._prop['datetime'].isoformat()
                tag.releasedate=tag.recording_date
            if self._is_dirty['title']:
                tag.title=self._prop['title']
            if self._is_dirty['cover']:
                tag.images.set(ImageFrame.FRONT_COVER,self.cover,"image/jpeg","Front Cover")

            # Write the new tag data to the MP3 file as ID3v2.4 so we can write
            # our datetime to the new high-precision TDRC and TDRL frames.
            tags.save(str(self.path),v2_version=4)

            # Clear all "dirty" flags.
            for k in self._is_dirty:
                self._is_dirty[k]=False

    def _set_prop(self,key,val):
        """
        Set the given property value. If we're not in a context, write it
        immediately to the MP3 file it came from. Otherwise, just mark
        this property as "dirty" so it will be written when the caller
        exits the current context.
        """

        if key not in self._prop:
            raise ValueError(f"\"{key}\" is not a property of {__class__.__name__}.")
        if self._prop[key]!=val:
            self._prop[key]=val
            self._is_dirty[key]=True
        if not self._in_context:
            self.save()

    @property
    def creator(self): return self._prop['creator']
    @creator.setter
    def creator(self,val): self._set_prop['creator',val]

    @property
    def podcast(self): return self._prop['podcast']
    @podcast.setter
    def podcast(self,val): self._set_prop['podcast',val]

    @property
    def episode(self): return self._prop['episode']
    @episode.setter
    def episode(self,val): self._set_prop['episode',val]

    @property
    def episode_num(self): return self._prop['episode'][0]
    @ episode_num.setter
    def episode_num(self,val):
        self._set_prop('episode',(val,self._prop['episode'][1]))

    @property
    def episode_total(self): return self._prop['episode'][1]
    @ episode_total.setter
    def episode_total(self,val):
        self._set_prop('episode',(self._prop['episode'][0],val))

    @property
    def datetime(self): return self._prop['datetime']
    @datetime.setter
    def datetime(self,val):
        if val.tzinfo is not None and val.tzinfo.utcoffset() is not None:
            self._set_prop['datetime',val]
        else:
            self._set_prop['datetime',val.replace(tzinfo=timezone.utc)]

    @property
    def title(self): return self._prop['title']
    @title.setter
    def title(self,val): self._set_prop['title',val]

    @property
    def cover(self): return self._prop['cover']
    @cover.setter
    def cover(self,val): self._set_prop['cover',val]

    @property
    def size(self): return self._prop['size']
    @size.setter
    def size(self,val): raise RuntimeError(f"{__class__.__name__}.size is read-only.")

    @contextmanager
    def update(self):
        """
        Use `with some_episode.update():` to open a context block for
        batching up several episode property updates to be written back
        to the MP3 file they came from.
        """

        self._in_batch=True
        try:
            yield self
        finally:
            self._in_batch=False
            self.save()

    def __str__(self):
        cover='Present' if self.cover else 'None'
        return f"""\
Path:    {self.path}
Podcast: {self.podcast}
Creator: {self.creator}
Episode: {self.episode}
Date:    {self.datetime}
Title:   {self.title}
Cover:   {cover}
"""

def tag_episodes(podcast_path):
    # Figure out what podcast we're working with.
    print(f"Updating ID3 data for episodes in {podcast_path} ...")
    if not podcast_path.is_dir():
        say(f"Directory not found: {podcast_path}",1)
    parts=podcast_path.parts[-2:]
    n=len(parts)
    if n!=2:
        say(f"Directory must have at least two parts. ({n} found.)",1)
    creator,podcast_name=parts
    print(f"{creator=}, {podcast_name=}")

    # Get our config file parameters.
    conf_file=podcast_path/"podcaster.toml"
    if not conf_file.is_file:
        say("Configuration file {conf_file} not found.",1)
    with open(conf_file,'rb') as f:
        conf=tomllib.load(f)
    group=conf.get('podcast',{})
    cover=group.get('cover')
    group=conf.get('episodes')
    start_time=group.get('start_time',"2026-01-01T12:00:00")
    start_time=datetime.fromisoformat(start_time).replace(tzinfo=timezone.utc)
    #time_interval=group.get("time_interval", dict(days=1))
    time_interval=timedelta(**time_interval)
    use_recording_date=group.get('use_recording_date',False)
    if use_recording_date:
        print("Using recording time of first track as time of the our first episode.")
    else:
        print(f"start_time={start_time.isoformat()}")
        print(f"{time_interval=}")

    # Get our list of podcast episodes so we can sort and iterate over them.
    episodes=[Episode(fn) for fn in sorted(podcast_path.glob('*.mp3'))]
    for ep in episodes:
        print(ep)

ap=ArgumentParser(
    prog=Path(sys.argv[0]).stem,
    description="Run a local RSS-based podcast service to let podcatchers on the local network download episodes from them. Podcasts and episodes are in a given directory structure on the local machine.",
)
ap.add_argument('--tag-episodes',metavar='DIR',action='store',help="Update the track, artist, album, release date, and album art according to the information in the podcasts.toml file in the given directory.")
ap.add_argument('--debug',action='store_true',help="Enable debug output.")
opt=ap.parse_args()

# Enable our DebugChannel if --debug was on the command line.
dc.enable(opt.debug)

def main():
    if opt.tag_episodes:
        try:
            d=Path(opt.tag_episodes).absolute()
        except Exception as e:
            raise
        tag_episodes(d)
    else:
        run_service()

if __name__ == "__main__":
    main()
