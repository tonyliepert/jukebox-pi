#!/usr/bin/env bash
# One-time installer: Wurlitzer jukebox -> Raspberry Pi -> OwnTone -> AirPlay 2.
# Safe to run more than once.
#
#   sudo ./install.sh
#
# Set OVERLAY=hifiberry-dacplusadc if you have the standard (non-Pro) DAC+ ADC:
#   sudo OVERLAY=hifiberry-dacplusadc ./install.sh

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Please run with sudo:  sudo ./install.sh"
    exit 1
fi
cd "$(dirname "$0")"

OVERLAY=${OVERLAY:-hifiberry-dacplusadcpro}
MUSIC_DIR=/srv/music
FIFO=$MUSIC_DIR/jukebox
OWNTONE_CONF=/etc/owntone.conf

CONFIG_TXT=/boot/firmware/config.txt
[[ -f $CONFIG_TXT ]] || CONFIG_TXT=/boot/config.txt

step() { echo; echo "==> $*"; }

# --- 1. Packages -------------------------------------------------------------
step "Installing packages"
apt-get update
apt-get install -y alsa-utils python3

if ! dpkg -s owntone >/dev/null 2>&1; then
    if apt-cache show owntone >/dev/null 2>&1; then
        apt-get install -y owntone
    else
        cat <<'EOF'

OwnTone isn't available from your current package sources.
Add the OwnTone repository for Raspberry Pi OS by following the instructions at
    https://owntone.github.io/owntone-server/installation/
then run this script again. Nothing else has been changed yet.
EOF
        exit 1
    fi
fi

# The streamer runs as its own unprivileged user. OwnTone runs as whatever
# account its config names (root in current Raspberry Pi OS packages).
id jukebox >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin jukebox
usermod -aG audio jukebox
OT_USER=$(sed -n 's/^[[:space:]]*uid[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$OWNTONE_CONF" | head -n1)
OT_USER=${OT_USER:-root}
OT_GROUP=$(id -gn "$OT_USER")
echo "OwnTone runs as '$OT_USER'; the streamer runs as 'jukebox'"

# --- 2. HiFiBerry sound card ---------------------------------------------------
step "Enabling the HiFiBerry ($OVERLAY) in $CONFIG_TXT"
cp -n "$CONFIG_TXT" "$CONFIG_TXT.bak-jukebox"
sed -i 's/^dtparam=audio=on/#dtparam=audio=on/' "$CONFIG_TXT"
sed -i 's/^dtoverlay=vc4-kms-v3d$/dtoverlay=vc4-kms-v3d,noaudio/' "$CONFIG_TXT"
if ! grep -q "^dtoverlay=$OVERLAY\$" "$CONFIG_TXT"; then
    printf '\n# Jukebox streamer: HiFiBerry sound card\ndtoverlay=%s\n' "$OVERLAY" >> "$CONFIG_TXT"
fi

# --- 3. OwnTone configuration --------------------------------------------------
step "Configuring OwnTone to watch $MUSIC_DIR for live pipes"
cp -n "$OWNTONE_CONF" "$OWNTONE_CONF.bak-jukebox"
python3 - "$OWNTONE_CONF" "$MUSIC_DIR" <<'PY'
import re, sys

path, music_dir = sys.argv[1], sys.argv[2]
text = open(path).read()

start = re.search(r'^library\s*\{', text, re.M)
if not start:
    sys.exit(f"Couldn't find the 'library {{' section in {path}")

# Find the closing brace of the library section.
depth, end = 0, None
for i in range(start.end() - 1, len(text)):
    if text[i] == '{':
        depth += 1
    elif text[i] == '}':
        depth -= 1
        if depth == 0:
            end = i
            break
if end is None:
    sys.exit(f"Couldn't find the end of the library section in {path}")

settings = {
    "directories": '{ "%s" }' % music_dir,
    "pipe_autostart": "true",
    "pipe_sample_rate": "44100",
    "pipe_bits_per_sample": "16",
}
MARKER = "# Added by jukebox-pi install.sh"
block = text[start.end():end]
for key in settings:            # drop existing (or commented-out) copies
    block = re.sub(r'^[ \t]*#?[ \t]*%s[ \t]*=.*\n' % key, '', block, flags=re.M)
block = re.sub(r'^[ \t]*%s\n' % re.escape(MARKER), '', block, flags=re.M)
block = block.lstrip("\n")
ours = f"\n\t{MARKER}\n" + "".join(
    f"\t{k} = {v}\n" for k, v in settings.items()) + "\n"
text = text[:start.end()] + ours + block + text[end:]

# Pick an available speaker whenever playback starts with none selected
# (e.g. library music started from a phone after a reboot).
text = re.sub(r'^[ \t]*#?[ \t]*speaker_autoselect[ \t]*=.*$',
              '\tspeaker_autoselect = yes', text, count=1, flags=re.M)

# The Pi has no local speaker: hide OwnTone's "Computer" output so autoselect
# can only choose network speakers.
audio = re.search(r'^audio\s*\{', text, re.M)
if audio:
    close = text.index('\n}', audio.end())
    body = re.sub(r'^[ \t]*#?[ \t]*type[ \t]*=[ \t]*"[a-z]*".*$', '\ttype = "disabled"',
                  text[audio.end():close], count=1, flags=re.M)
    text = text[:audio.end()] + body + text[close:]

open(path, "w").write(text)
PY

# --- 4. Pipe for the live audio -------------------------------------------------
step "Creating the live audio pipe $FIFO"
mkdir -p "$MUSIC_DIR"
[[ -p $FIFO ]] || mkfifo "$FIFO"
chown jukebox:"$OT_GROUP" "$FIFO"       # streamer writes, OwnTone reads
chmod 660 "$FIFO"

# --- 5. Streaming service -------------------------------------------------------
step "Installing the jukebox-stream service"
install -m 755 jukebox-stream.py /usr/local/bin/jukebox-stream.py
if [[ -f /etc/jukebox-stream.conf ]]; then
    install -m 644 jukebox-stream.conf /etc/jukebox-stream.conf.new
    echo "Kept your existing /etc/jukebox-stream.conf (new defaults saved as .new)"
else
    install -m 644 jukebox-stream.conf /etc/jukebox-stream.conf
fi
install -m 644 jukebox-stream.service /etc/systemd/system/jukebox-stream.service
systemctl daemon-reload
systemctl enable owntone jukebox-stream
systemctl restart owntone

cat <<EOF

Done. Next:
  1. Reboot so the HiFiBerry is detected:   sudo reboot
  2. Set the ADC input and gain:            see README.md, "After installing"
  3. Choose the S1000W in OwnTone:          http://$(hostname).local:3689
EOF
