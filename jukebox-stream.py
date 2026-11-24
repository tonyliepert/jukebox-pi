#!/usr/bin/env python3
"""
jukebox-stream: capture the Wurlitzer's aux output and feed it to OwnTone.

Reads live audio from the HiFiBerry ADC with `arecord` and writes it into a
named pipe (FIFO) in OwnTone's library. When a record starts, the streamer
puts the pipe into OwnTone's queue through its JSON API and plays it. OwnTone
streams it to the selected AirPlay 2 speakers (the Edifier S1000W). OwnTone's
own pipe_autostart must be off, because it clears the queue.

Modes (set MODE in /etc/jukebox-stream.conf):
  gated   Stream only while music is playing. A session starts when the
          level stays above START_THRESHOLD_DB for START_HOLD_SECONDS, and
          ends after STOP_AFTER_SECONDS below STOP_THRESHOLD_DB. Then the next
          queued song plays, or OwnTone stops and frees the speakers.
  always  Stream continuously, silence included.

Run with --monitor to print live input levels while you set the ADC gain and
thresholds. Stop the service first, because only one program can use the
input at a time:
    sudo systemctl stop jukebox-stream
    jukebox-stream.py --monitor
"""

import array
import collections
import errno
import json
import math
import os
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request


def env(name, default, cast=str):
    value = os.environ.get(name, "").strip()
    return cast(value) if value else default


DEVICE = env("CAPTURE_DEVICE", "hw:CARD=sndrpihifiberry,DEV=0")
RATE = env("SAMPLE_RATE", 44100, int)
FIFO = env("FIFO", "/srv/music/jukebox")
MODE = env("MODE", "gated").lower()
START_DB = env("START_THRESHOLD_DB", -40.0, float)
STOP_DB = env("STOP_THRESHOLD_DB", -50.0, float)
START_HOLD = env("START_HOLD_SECONDS", 0.3, float)
STOP_AFTER = env("STOP_AFTER_SECONDS", 30.0, float)
PREROLL = env("PREROLL_SECONDS", 1.0, float)
SPEAKERS = [s.strip() for s in env("SPEAKERS", "").split(",") if s.strip()]
OWNTONE_API = env("OWNTONE_API", "http://localhost:3689").rstrip("/")
CHECK_SECONDS = 1                           # OwnTone session checks

CHANNELS = 2
BYTES_PER_FRAME = CHANNELS * 2              # 16-bit stereo
FRAMES_PER_CHUNK = 2048
BYTES_PER_CHUNK = FRAMES_PER_CHUNK * BYTES_PER_FRAME
CHUNK_SECONDS = FRAMES_PER_CHUNK / RATE
PIPE_WRITE_SIZE = 4096                      # = PIPE_BUF on Linux: writes are atomic


def log(msg):
    print(msg, flush=True)


def peak_db(chunk):
    """Peak level of a chunk of S16_LE samples, in dBFS."""
    samples = array.array("h")
    samples.frombytes(chunk)
    if sys.byteorder != "little":
        samples.byteswap()
    peak = max(max(samples), -min(samples))
    return 20 * math.log10(peak / 32768) if peak else -120.0


def capture_chunks():
    """Yield fixed-size chunks of raw audio from arecord, forever."""
    cmd = ["arecord", "-q", "-D", DEVICE, "-f", "S16_LE", "-r", str(RATE),
           "-c", str(CHANNELS), "-t", "raw"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)
    try:
        while True:
            buf = bytearray()
            while len(buf) < BYTES_PER_CHUNK:
                data = proc.stdout.read(BYTES_PER_CHUNK - len(buf))
                if not data:
                    raise RuntimeError(
                        f"arecord stopped (exit code {proc.wait()}). Check that "
                        f"CAPTURE_DEVICE={DEVICE} exists (arecord -l) and that no "
                        "other program is using it.")
                buf += data
            yield bytes(buf)
    finally:
        if proc.poll() is None:
            proc.terminate()


class FifoWriter:
    """Non-blocking writer for OwnTone's pipe. Never stalls the capture loop."""

    def __init__(self, path):
        self.path = path
        self.fd = None

    def open(self):
        """Open the pipe. Returns False if OwnTone isn't reading it."""
        try:
            self.fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            if e.errno == errno.ENXIO:      # no reader on the other end
                return False
            raise
        return True

    def write(self, chunk):
        """Write a chunk. Returns False if OwnTone closed its end."""
        try:
            for i in range(0, len(chunk), PIPE_WRITE_SIZE):
                # Each piece is <= PIPE_BUF, so it is written whole or not at
                # all. That keeps stereo frames aligned even when data is dropped.
                os.write(self.fd, chunk[i:i + PIPE_WRITE_SIZE])
        except BlockingIOError:
            pass                            # pipe full (OwnTone paused): drop audio
        except BrokenPipeError:
            self.close()
            return False
        return True

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class OwnToneController(threading.Thread):
    """Drives OwnTone for each jukebox session.

    OwnTone's own pipe autostart can't be used: whenever playback leaves the
    pipe while audio is arriving, autostart clears the whole queue, which
    wipes out songs picked from the library during a record. Instead:

    Session start: insert the jukebox entry right after whatever is playing
    (or at the end of the queue) and play it. Nothing queued is removed.

    During the session (checked every second):
    - If a library song is started (an MPD client such as Rigelian inserts
      the song and plays it straight away), put the jukebox back just before
      that song and play it, so the song plays when the record ends.
    - If someone presses stop or pause, leave it.
    - Select the configured speakers until they're confirmed, then back off,
      so Spotify, Alexa or a person can take the speaker.

    Session end: remove the jukebox entry and play the next queued song, or
    stop to release the speakers. OwnTone treats the pipe as an endless track,
    so on its own it would just sit there.

    Runs in its own thread so a slow API call can never stall the audio.
    """

    def __init__(self, names):
        super().__init__(daemon=True)
        self.names = names
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.active = False         # a jukebox session is in progress
        self.claimed = False        # speakers confirmed selected this session
        self.pending_start = False  # put the jukebox on at the next pass
        self.pending_stop = False   # end the session at the next pass
        self.pipe_uri = None
        self.reported = set()       # messages already logged this session

    def start_session(self):
        with self.lock:
            self.active, self.claimed = True, False
            self.pending_start, self.pending_stop = True, False
            self.reported.clear()
        self.wake.set()

    def stop_session(self):
        with self.lock:
            self.active, self.pending_start, self.pending_stop = False, False, True
        self.wake.set()

    def note(self, msg):
        if msg not in self.reported:
            self.reported.add(msg)
            log(msg)

    @staticmethod
    def api(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(OWNTONE_API + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    # --- queue helpers --------------------------------------------------------

    def snapshot(self):
        player = self.api("GET", "/api/player")
        items = self.api("GET", "/api/queue").get("items", [])
        return player, items

    @staticmethod
    def is_jukebox(item):
        return item.get("path") == FIFO

    def jukebox_uri(self):
        if not self.pipe_uri:
            expr = urllib.parse.quote(f'path is "{FIFO}"')
            found = self.api("GET", f"/api/search?type=tracks&expression={expr}")
            tracks = found.get("tracks", {}).get("items", [])
            if not tracks:
                raise RuntimeError(f"OwnTone's library has no entry for {FIFO}")
            self.pipe_uri = tracks[0]["uri"]
        return self.pipe_uri

    def remove_jukebox_entries(self, items, keep=None):
        for i in items:
            if self.is_jukebox(i) and i["id"] != keep:
                self.api("DELETE", f"/api/queue/items/{i['id']}")

    def play_jukebox_at(self, position):
        """Insert a jukebox entry at a queue position and play it."""
        uri = urllib.parse.quote(self.jukebox_uri())
        self.api("POST", f"/api/queue/items/add?uris={uri}&position={position}")
        _, items = self.snapshot()
        entry = max((i for i in items if self.is_jukebox(i)), key=lambda i: i["id"])
        self.api("PUT", f"/api/player/play?item_id={entry['id']}")

    # --- session steps --------------------------------------------------------

    def start_jukebox(self):
        player, items = self.snapshot()
        state, current_id = player.get("state"), player.get("item_id")
        if state == "play" and any(i["id"] == current_id and self.is_jukebox(i) for i in items):
            return                          # already playing (e.g. after a restart)
        self.remove_jukebox_entries(items)  # leftovers from an interrupted session
        player, items = self.snapshot()
        current = next((i for i in items if i["id"] == player.get("item_id")), None)
        if current is not None and player.get("state") != "stop":
            position = current["position"] + 1   # right after what was playing
        else:
            position = len(items)
        self.play_jukebox_at(position)
        log("Jukebox playing in OwnTone")

    def guard(self):
        """Keep the record playing if a library song was started over it."""
        player, items = self.snapshot()
        if player.get("state") == "stop":
            return                          # stopped on purpose: leave it
        current = next((i for i in items if i["id"] == player.get("item_id")), None)
        if current is None or self.is_jukebox(current):
            return                          # still the record (pausing it is fine too)
        # A library song is now current. It may show as paused for a moment
        # while it loads, so don't wait for "play" before switching back.
        self.remove_jukebox_entries(items)
        player, items = self.snapshot()
        current = next((i for i in items if i["id"] == player.get("item_id")), current)
        self.play_jukebox_at(current["position"])   # just before the picked song
        log(f"'{current.get('title', '?')}' was picked during the record; "
            "it will play when the record ends")

    def end_session(self):
        player, items = self.snapshot()
        jukebox = [i for i in items if self.is_jukebox(i)]
        current = player.get("item_id")
        on_jukebox = any(i["id"] == current for i in jukebox)
        state = player.get("state")
        if state != "stop" and not on_jukebox:
            log("OwnTone is playing something else; left it running")
        elif on_jukebox:
            pos = next(i["position"] for i in jukebox if i["id"] == current)
            following = [i for i in items
                         if i.get("position", -1) > pos and not self.is_jukebox(i)]
            if following:
                self.api("PUT", f"/api/player/play?item_id={following[0]['id']}")
                log(f"Record finished; playing next in queue: "
                    f"{following[0].get('artist', '?')} - {following[0].get('title', '?')}")
            else:
                self.api("PUT", "/api/player/stop")
                log("Stopped OwnTone playback (speakers released)")
        self.remove_jukebox_entries(items)

    def claim_speakers(self):
        """Select any configured speaker that isn't selected.

        Returns True only when every speaker was already selected on this
        pass, so a selection is confirmed on the following check before
        backing off.
        """
        outputs = self.api("GET", "/api/outputs")["outputs"]
        all_selected = True
        for name in self.names:
            match = ([o for o in outputs if o["name"] == name]
                     or [o for o in outputs if o["name"].startswith(name)])
            if not match:
                self.note(f"Waiting for speaker '{name}' to appear on the network")
                all_selected = False
            elif not match[0]["selected"]:
                self.api("PUT", f"/api/outputs/{match[0]['id']}", {"selected": True})
                log(f"Selected speaker '{match[0]['name']}'")
                all_selected = False
        return all_selected

    def run(self):
        while True:
            self.wake.wait(CHECK_SECONDS)
            self.wake.clear()
            with self.lock:
                active, claimed = self.active, self.claimed
                start, stop = self.pending_start, self.pending_stop
                self.pending_start = self.pending_stop = False
            try:
                if stop:
                    self.end_session()
                if start:
                    self.start_jukebox()
                elif active:
                    self.guard()
                if active and not claimed and self.names and self.claim_speakers():
                    with self.lock:
                        if self.active:
                            self.claimed = True
                    log("Speakers connected; leaving speaker choice alone "
                        "until the next session")
            except Exception as e:      # OwnTone restarting, network hiccup, etc.
                if start:
                    with self.lock:
                        if self.active:
                            self.pending_start = True   # try again next pass
                self.note(f"Couldn't reach OwnTone's API: {e}")


def make_controller():
    controller = OwnToneController(SPEAKERS)
    controller.start()
    if SPEAKERS:
        log("Selecting these speakers at the start of each session: "
            + ", ".join(SPEAKERS))
    return controller


def ensure_fifo():
    if not os.path.exists(FIFO):
        os.mkfifo(FIFO, 0o660)
    elif not stat.S_ISFIFO(os.stat(FIFO).st_mode):
        raise RuntimeError(f"{FIFO} exists but is not a named pipe")


def run_gated(chunks, writer, controller):
    preroll = collections.deque(maxlen=max(1, round(PREROLL / CHUNK_SECONDS)))
    session = connected = False
    loud_for = quiet_for = 0.0

    log(f"Gated mode: start above {START_DB} dBFS, "
        f"stop after {STOP_AFTER:g} s below {STOP_DB} dBFS")
    for chunk in chunks:
        level = peak_db(chunk)

        if not session:
            preroll.append(chunk)
            loud_for = loud_for + CHUNK_SECONDS if level >= START_DB else 0.0
            if loud_for >= START_HOLD:
                log(f"Music detected ({level:.1f} dBFS): jukebox session started")
                session, connected, quiet_for = True, False, 0.0
                controller.start_session()
            continue

        quiet_for = quiet_for + CHUNK_SECONDS if level < STOP_DB else 0.0
        if quiet_for >= STOP_AFTER:
            writer.close()
            log(f"Silent for {STOP_AFTER:g} s: jukebox session ended")
            session, loud_for = False, 0.0
            preroll.clear()
            controller.stop_session()
            continue

        if writer.fd is None:
            # OwnTone opens the pipe once the controller has started the jukebox,
            # and again if it's switched back after a song was picked.
            preroll.append(chunk)
            if writer.open():
                for c in preroll:           # include the moments before (re)connecting
                    writer.write(c)
                preroll.clear()
                if not connected:
                    log("Streaming to OwnTone")
                    connected = True
            continue
        if not writer.write(chunk):
            preroll.clear()                 # OwnTone stopped reading; reconnect later


def run_always(chunks, writer, controller):
    log("Always mode: streaming continuously")
    controller.start_session()
    connected = False
    for chunk in chunks:
        if writer.fd is None:
            if not writer.open():
                continue
            if not connected:
                log("Streaming to OwnTone")
                connected = True
        writer.write(chunk)


def run_monitor(chunks):
    log("Input peak level in dBFS (Ctrl+C to stop).")
    log(f"Thresholds: start {START_DB} dBFS, stop {STOP_DB} dBFS. "
        "Aim for loud passages around -12 to -3 dBFS.")
    peak, elapsed = -120.0, 0.0
    for chunk in chunks:
        peak = max(peak, peak_db(chunk))
        elapsed += CHUNK_SECONDS
        if elapsed >= 0.5:
            bar = "#" * int(max(0.0, peak + 60))        # -60..0 dBFS -> 0..60 chars
            note = "  CLIPPING - reduce gain" if peak > -0.5 else ""
            log(f"{peak:6.1f} dBFS |{bar:<60}|{note}")
            peak, elapsed = -120.0, 0.0


def main():
    try:
        chunks = capture_chunks()
        if "--monitor" in sys.argv[1:]:
            run_monitor(chunks)
            return 0
        ensure_fifo()
        writer = FifoWriter(FIFO)
        controller = make_controller()
        if MODE == "always":
            run_always(chunks, writer, controller)
        elif MODE == "gated":
            run_gated(chunks, writer, controller)
        else:
            log(f"Unknown MODE '{MODE}' (use 'gated' or 'always')")
            return 2
    except KeyboardInterrupt:
        return 0
    except RuntimeError as e:
        log(f"Error: {e}")
        time.sleep(1)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
