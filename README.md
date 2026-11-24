# Jukebox Pi streamer

Streams a Wurlitzer 1015 OMT jukebox's auxiliary output (5-pin DIN, pins
1/4/2) to Wi-Fi speakers in another room over AirPlay 2. It uses a Raspberry
Pi 4, a HiFiBerry DAC+ ADC Pro and [OwnTone](https://owntone.github.io/owntone-server/).
Built and tested with Edifier S1000W speakers.

```
Jukebox DIN → HiFiBerry ADC → jukebox-stream.py → pipe → OwnTone → AirPlay 2 → speakers
```

Streaming starts on its own when a record plays and stops after 45 seconds of
silence. That's long enough to cover the carousel loading the next record.
When the jukebox is idle the speakers are free for Spotify, Alexa and so on.

## Files

| File | Installed to | Purpose |
|---|---|---|
| `install.sh` | — | One-time setup. Run on the Pi with `sudo`. Safe to run again. |
| `jukebox-stream.py` | `/usr/local/bin/` | Records from the ADC, feeds OwnTone's pipe, starts and stops the stream, and selects the speakers |
| `jukebox-stream.conf` | `/etc/jukebox-stream.conf` | Settings: capture device, mode, thresholds, speakers |
| `jukebox-stream.service` | `/etc/systemd/system/` | Runs the streamer at boot and restarts it if it stops |
| `Wurlitzer_Jukebox_Streaming_Guide.pdf` | — | Background and planning guide: options, parts list, wiring. Written before the build, so where they differ, this README wins. |

## Parts

- Raspberry Pi 4 (1 GB is plenty), official 5.1 V 3 A USB-C power supply,
  32 GB microSD card
- HiFiBerry DAC+ ADC Pro (the DAC2 ADC Pro also works). It needs the **ADC**
  input. A DAC-only board won't work.
- A plastic case if the Pi uses Wi-Fi. A steel case blocks the signal.
- A DIN-to-3.5 mm cable (see Wiring)

## Wiring

The jukebox's aux socket is labelled *Zusatzverstärker Ein-/Ausgang*.
Its outputs are on **pins 1 and 4**, not 3 and 5. This was traced from the
amplifier schematic. The output is line level (about 0.1–0.3 V), taken before
the volume control, and stays stereo whatever the Mono/Stereo switch is set to.

| DIN pin | Signal | 3.5 mm plug (HiFiBerry input) |
|---|---|---|
| 1 | Left output | Tip |
| 4 | Right output | Ring |
| 2 | Ground | Sleeve (and cable shield) |
| 3, 5 | Amplifier inputs | Not connected |

With a ready-made DIN to 4× RCA cable, use the plugs for pins 1 and 4 and tape
over the other two.

## Installing

1. Write **Raspberry Pi OS Lite (64-bit)** to the microSD card with
   Raspberry Pi Imager. Set the hostname to `jukebox`, a user, your Wi-Fi and
   SSH. Boot the Pi. The first boot takes 3–5 minutes.
2. Add OwnTone's package repository. OwnTone isn't in the standard Raspberry
   Pi OS packages. These commands are for Raspberry Pi OS based on Debian 13
   "trixie"; for other releases see the
   [OwnTone install page](https://owntone.github.io/owntone-server/installation/).
   ```bash
   B=https://raw.githubusercontent.com/owntone/owntone-apt/refs/heads/master/repo/rpi
   wget -q -O - $B/owntone.gpg | sudo gpg --dearmor --output /usr/share/keyrings/owntone-archive-keyring.gpg
   sudo wget -q -O /etc/apt/sources.list.d/owntone.list $B/owntone-trixie.list
   ```
3. Copy the project files to the Pi. From PowerShell, in this folder:
   ```powershell
   ssh <user>@jukebox.local "mkdir -p ~/jukebox-pi"
   scp install.sh jukebox-stream.py jukebox-stream.conf jukebox-stream.service README.md <user>@jukebox.local:jukebox-pi/
   ```
4. Run the installer, then reboot:
   ```bash
   ssh <user>@jukebox.local
   cd ~/jukebox-pi && sudo bash install.sh
   sudo reboot
   ```
   For the standard (non-Pro) DAC+ ADC, use
   `sudo OVERLAY=hifiberry-dacplusadc bash install.sh` instead.

The installer installs OwnTone and turns on the HiFiBerry. It configures OwnTone
to watch `/srv/music` for live pipes, creates the pipe, adds a `jukebox`
system user for the streamer and turns on the service. It backs up
`/etc/owntone.conf` and `/boot/firmware/config.txt` first, as `*.bak-jukebox`.

## After installing

### 1. Input and gain

These settings worked on the 1015 OMT. Note that `amixer` needs `--` before a
negative value.

```bash
C=sndrpihifiberry
amixer -c $C sset "ADC Left Input" "VINL1[SE]"       # line-in jack
amixer -c $C sset "ADC Right Input" "VINR1[SE]"
amixer -c $C sset "ADC Mic Bias" "Mic Bias off"      # no DC on a line input
amixer -c $C -- sset "PGA Gain Left" "-3.0dB"
amixer -c $C -- sset "PGA Gain Right" "-3.0dB"
sudo alsactl store                                   # keep across reboots
```

To check the levels yourself, stop the service, play a record and watch the
input:

```bash
sudo systemctl stop jukebox-stream
/usr/local/bin/jukebox-stream.py --monitor          # Ctrl+C to stop
sudo systemctl start jukebox-stream
```

Aim for loud passages to peak around -6 dBFS with no "CLIPPING" warning. With
-3 dB of gain the OMT peaked around -7 dBFS. The idle hum measured about
-67 dBFS.

### 2. Speakers

Set `SPEAKERS=` in `/etc/jukebox-stream.conf` to the start of the speaker's
name as OwnTone shows it, for example `EDIFIER_S1000W`. Separate several
names with commas. You can see the names at **http://jukebox.local:3689**.
Set the volume there too, because the jukebox's own volume knob doesn't affect
this output.

How the streamer handles the speakers:

| Situation | What happens |
|---|---|
| Jukebox idle | The Pi leaves the speakers alone |
| A record starts | The streamer selects the speakers, retrying every 5 s until they're connected. After a power cut the speaker often appears on the network a few seconds late, and OwnTone won't pick it up on its own. |
| Spotify or Alexa is started during a record | It takes the speaker. The streamer doesn't take it back. |
| 45 s of silence | The streamer removes the jukebox entry from OwnTone's queue. If songs were queued during the record, the next one plays. Otherwise OwnTone stops and releases the speakers. OwnTone treats the jukebox as an endless track, so on its own it would just wait. |

### 3. Start and stop tuning (optional)

Edit `/etc/jukebox-stream.conf`, then `sudo systemctl restart jukebox-stream`.

- `STOP_THRESHOLD_DB` (-50): a few dB above the idle hum
- `START_THRESHOLD_DB` (-40): about 10 dB above that
- `STOP_AFTER_SECONDS` (45): longer than the longest record change
- `MODE=always`: stream continuously instead

## Checking and troubleshooting

```bash
journalctl -u jukebox-stream -f        # "Music detected", "Selected speaker", "streaming stopped"
systemctl status owntone jukebox-stream
sudo tail /var/log/owntone.log
```

| Symptom | Likely cause |
|---|---|
| `arecord stopped` in the log | HiFiBerry not detected (check `arecord -l` and reboot), or another program such as `--monitor` is using the input |
| `--monitor` shows about -120 dBFS with music playing | Wrong ADC input selected, or the cable is on pins 3/5 instead of 1/4 |
| "OwnTone is not reading the pipe" just after boot | Harmless: the streamer started a second before OwnTone. It recovers on its own. |
| "Waiting for speaker ... to appear" | The speaker is off, or on a different network or VLAN from the Pi (for example a router's separate IoT network) |
| Music detected but speakers silent | The speaker isn't on its Wi-Fi input, or `SPEAKERS=` doesn't match its name in OwnTone |
| Stream never stops | The idle hum is above `STOP_THRESHOLD_DB`. Measure it with `--monitor` and raise the threshold. |
| Stream stops between records | Raise `STOP_AFTER_SECONDS` |
| Hum | Add a ground-loop isolator between the jukebox and the HiFiBerry |

## Playing a music library (optional)

OwnTone is also a music server, so the Pi can play your own music files to
the same speakers. Put the music on a USB stick, plug it into the Pi and mount
it **read-only** inside `/srv/music`, which OwnTone already scans. Use the
stick's UUID from `lsblk -f`:

```bash
sudo mkdir -p /srv/music/usb
echo "UUID=XXXX-XXXX  /srv/music/usb  vfat  ro,nofail,utf8,uid=0,gid=0,umask=022,x-systemd.device-timeout=10s  0  0" | sudo tee -a /etc/fstab
sudo mkdir -p /etc/systemd/system/owntone.service.d
printf "[Unit]\nAfter=srv-music-usb.mount\n" | sudo tee /etc/systemd/system/owntone.service.d/after-usb.conf
sudo systemctl daemon-reload && sudo mount /srv/music/usb
curl -X PUT localhost:3689/api/update          # scan now
```

- `nofail` lets the Pi boot normally without the stick, and `utf8` keeps
  accented names such as "König" correct.
- Use `exfat` or `ntfs3` instead of `vfat` for those formats.
- Browse and play at **http://jukebox.local:3689**, or use an MPD app such as
  MALP (Android) or Rigelian (iPhone).
- After adding music to the stick, plug it back in and click **Update
  library** on the web page.

The installer turns on OwnTone's `speaker_autoselect` and hides its local
"Computer" output (`type = "disabled"` in the `audio` section). OwnTone
forgets the selected speaker after a reboot, so this lets library music
started from a phone pick the network speaker automatically.

A record starting on the jukebox takes over from library music. Songs you
add to the queue during a record (for example "Add to Queue" in Rigelian)
play once the jukebox goes quiet. If you switch to library music during a
record, it keeps playing.

## Protecting the SD card (optional)

If the Pi loses power whenever the jukebox is switched off, turn on
`sudo raspi-config` → Performance Options → Overlay File System once
everything is set up. That makes the card read-only. Settings changes aren't
kept while it's on.

## Undoing

```bash
sudo systemctl disable --now jukebox-stream
sudo rm /etc/systemd/system/jukebox-stream.service /usr/local/bin/jukebox-stream.py /etc/jukebox-stream.conf
sudo userdel jukebox
sudo cp /etc/owntone.conf.bak-jukebox /etc/owntone.conf
sudo cp /boot/firmware/config.txt.bak-jukebox /boot/firmware/config.txt
sudo apt remove owntone && sudo rm /etc/apt/sources.list.d/owntone.list /usr/share/keyrings/owntone-archive-keyring.gpg
```

## License

MIT. See [LICENSE](LICENSE). You are free to use, change and share this project.
