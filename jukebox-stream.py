#!/usr/bin/env python3
"""
jukebox-stream: capture the Wurlitzer's aux output and feed it to OwnTone.

Reads live audio from the HiFiBerry ADC with `arecord` and writes it into a
named pipe (FIFO) that OwnTone watches. OwnTone streams whatever arrives in
the pipe to the selected AirPlay 2 speakers (the Edifier S1000W).

Modes (set MODE in /etc/jukebox-stream.conf):
  gated   Stream only while music is playing. Streaming starts when the
          level stays above START_THRESHOLD_DB for START_HOLD_SECONDS, and
          stops after STOP_AFTER_SECONDS below STOP_THRESHOLD_DB. Stopping
          closes the pipe, which tells OwnTone to stop and frees the speakers.
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
SPEAKER_CHECK_SECONDS = 5

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


class SpeakerKeeper(threading.Thread):
    """Manages OwnTone's speakers around each jukebox session.

    Session start: keep selecting the configured speakers until all of them
    are confirmed selected. OwnTone only re-selects remembered speakers if they
    are already visible when playback starts, and after a power cut the speaker
    is often found a few seconds later, so this retries every few seconds.

    During the session: once the speakers are confirmed, back off. If Spotify,
    Alexa or a person takes the speaker or deselects it, leave it alone.

    Session end: remove the jukebox entry from OwnTone's queue, then play the
    next queued song if there is one, or stop to release the speakers (OwnTone
    would otherwise wait on the pipe forever). If someone switched to library
    music, leave it playing.

    Runs in its own thread so a slow API call can never stall the audio.
    """

    def __init__(self, names):
        super().__init__(daemon=True)
        self.names = names
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.active = False         # a jukebox session is in progress
        self.claimed = False        # speakers confirmed selected this session
        self.pending_stop = False   # tell OwnTone to stop at the next pass
        self.reported = set()       # messages already logged this session

    def start_session(self):
        with self.lock:
            self.active, self.claimed, self.pending_stop = True, False, False
            self.reported.clear()
        self.wake.set()

    def stop_session(self):
        with self.lock:
            self.active, self.pending_stop = False, True
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

    def end_session(self):
        """Hand OwnTone back to normal use after the jukebox goes quiet.

        OwnTone treats the pipe as an endless track: when the record ends it
        just waits on it and never moves on to the rest of the queue. So:
        - if OwnTone is playing something else (someone switched to library
          music), leave it alone;
        - otherwise remove the jukebox entry from the queue (a new record adds
          it back), then play the next queued song if there is one, or stop
          to release the speakers.
        """
        player = self.api("GET", "/api/player")
        items = self.api("GET", "/api/queue").get("items", [])
        pipe = [i for i in items if i.get("path") == FIFO]
        current = player.get("item_id")
        playing_jukebox = (player.get("state") != "stop"
                           and any(i.get("id") == current for i in pipe))
        if player.get("state") != "stop" and not playing_jukebox:
            log("OwnTone is playing something else; left it running")
        elif playing_jukebox:
            pos = next(i["position"] for i in pipe if i["id"] == current)
            following = [i for i in items
                         if i.get("position", -1) > pos and i.get("path") != FIFO]
            if following:
                self.api("PUT", f"/api/player/play?item_id={following[0]['id']}")
                log(f"Record finished; playing next in queue: "
                    f"{following[0].get('artist', '?')} - {following[0].get('title', '?')}")
            else:
                self.api("PUT", "/api/player/stop")
                log("Stopped OwnTone playback (speakers released)")
        for i in pipe:                  # keep the silent jukebox entry out of the queue
            self.api("DELETE", f"/api/queue/items/{i['id']}")

    def claim_speakers(self):
        """Select any configured speaker that isn't selected.

        Returns True only when every speaker was already selected on this
        pass, so a selection is confirmed on the following check before the
        keeper backs off.
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
            self.wake.wait(SPEAKER_CHECK_SECONDS)
            self.wake.clear()
            with self.lock:
                active, claimed, stop = self.active, self.claimed, self.pending_stop
                self.pending_stop = False
            try:
                if stop:
                    self.end_session()
                if active and not claimed and self.names and self.claim_speakers():
                    with self.lock:
                        if self.active:
                            self.claimed = True
                    log("Speakers connected; leaving speaker choice alone "
                        "until the next session")
            except Exception as e:      # OwnTone restarting, network hiccup, etc.
                self.note(f"Couldn't reach OwnTone's API: {e}")


def make_keeper():
    keeper = SpeakerKeeper(SPEAKERS)
    keeper.start()
    if SPEAKERS:
        log("Selecting these speakers at the start of each session: "
            + ", ".join(SPEAKERS))
    return keeper


def ensure_fifo():
    if not os.path.exists(FIFO):
        os.mkfifo(FIFO, 0o660)
    elif not stat.S_ISFIFO(os.stat(FIFO).st_mode):
        raise RuntimeError(f"{FIFO} exists but is not a named pipe")


def run_gated(chunks, writer, keeper=None):
    preroll = collections.deque(maxlen=max(1, round(PREROLL / CHUNK_SECONDS)))
    streaming = False
    loud_for = quiet_for = 0.0
    warned_no_reader = False

    log(f"Gated mode: start above {START_DB} dBFS, "
        f"stop after {STOP_AFTER:g} s below {STOP_DB} dBFS")
    for chunk in chunks:
        level = peak_db(chunk)

        if not streaming:
            preroll.append(chunk)
            loud_for = loud_for + CHUNK_SECONDS if level >= START_DB else 0.0
            if loud_for < START_HOLD:
                continue
            if not writer.open():
                if not warned_no_reader:
                    log("Music detected, but OwnTone is not reading the pipe. "
                        "Is owntone running with pipe_autostart = true?")
                    warned_no_reader = True
                continue
            log(f"Music detected ({level:.1f} dBFS): streaming started")
            for c in preroll:               # include the moments before detection
                writer.write(c)
            preroll.clear()
            streaming, quiet_for, warned_no_reader = True, 0.0, False
            if keeper:
                keeper.start_session()
            continue

        if not writer.write(chunk):
            log("OwnTone closed the pipe: streaming stopped")
            streaming, loud_for = False, 0.0
            if keeper:
                keeper.stop_session()
            continue
        quiet_for = quiet_for + CHUNK_SECONDS if level < STOP_DB else 0.0
        if quiet_for >= STOP_AFTER:
            writer.close()                  # EOF tells OwnTone to stop playback
            log(f"Silent for {STOP_AFTER:g} s: streaming stopped")
            streaming, loud_for = False, 0.0
            if keeper:
                keeper.stop_session()


def run_always(chunks, writer, keeper=None):
    log("Always mode: streaming continuously")
    waiting_logged = False
    for chunk in chunks:
        if writer.fd is None:
            if not writer.open():
                if not waiting_logged:
                    log("Waiting for OwnTone to read the pipe...")
                    waiting_logged = True
                continue
            log("Connected to OwnTone: streaming")
            waiting_logged = False
            if keeper:
                keeper.start_session()
        if not writer.write(chunk):
            log("OwnTone closed the pipe; will reconnect")
            if keeper:
                keeper.stop_session()


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
        keeper = make_keeper()
        if MODE == "always":
            run_always(chunks, writer, keeper)
        elif MODE == "gated":
            run_gated(chunks, writer, keeper)
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
