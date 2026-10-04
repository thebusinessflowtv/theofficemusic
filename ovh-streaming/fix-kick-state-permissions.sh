#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${MEDIAFORGE_REPO:-/home/ubuntu/theofficemusic}"
OVH_DIR="$REPO_DIR/ovh-streaming"
STATE="$OVH_DIR/state"
KICK="$STATE/kick"
KICK_DJ_AUDIO="$STATE/kick-dj-audio"
OWNER="${SUDO_USER:-${USER:-ubuntu}}"

# On this VPS the interactive/deploy user is ubuntu. Resolve safely if invoked via sudo.
if [ "$OWNER" = "root" ] || ! id "$OWNER" >/dev/null 2>&1; then
  OWNER="ubuntu"
fi
GROUP="$(id -gn "$OWNER")"

sudo -n mkdir -p   "$KICK/audio-commands"   "$KICK/playlist-backups"   "$KICK_DJ_AUDIO"

# Immediate repair for existing root-created files/directories.
sudo -n chown -R "$OWNER:$GROUP"   "$KICK/audio-commands"   "$KICK/playlist-backups"   "$KICK_DJ_AUDIO"

# These top-level files are intentionally mutable by both the runtime and host ops.
for p in   "$KICK/playlist.json"   "$KICK/desired.json"   "$KICK/command.json"   "$KICK/kick-dj-copy-result.json"
do
  if [ -e "$p" ]; then
    sudo -n chown "$OWNER:$GROUP" "$p"
    sudo -n chmod u+rw,g+rw "$p"
  fi
done

sudo -n chmod -R u+rwX,g+rwX   "$KICK/audio-commands"   "$KICK/playlist-backups"   "$KICK_DJ_AUDIO"

# Keep the group on newly-created children.
sudo -n chmod g+s   "$KICK/audio-commands"   "$KICK/playlist-backups"   "$KICK_DJ_AUDIO"

# Default ACL prevents future root-created runtime files from locking ubuntu out.
# No package installation or service/container restart is performed.
if command -v setfacl >/dev/null 2>&1; then
  for d in "$KICK/audio-commands" "$KICK/playlist-backups" "$KICK_DJ_AUDIO"; do
    sudo -n setfacl -Rm "u:$OWNER:rwX,m::rwX" "$d"
    sudo -n setfacl -m "d:u:$OWNER:rwx,d:m:rwx" "$d"
  done
  acl_mode="default-acl"
else
  acl_mode="group-write-fallback"
fi

# Smoke test: create and remove a file as the normal host user.
probe="$KICK/audio-commands/.permission-probe-$$.tmp"
printf 'ok\n' > "$probe"
rm -f "$probe"

echo "KICK_STATE_PERMISSIONS_OK owner=$OWNER group=$GROUP mode=$acl_mode"
echo "No container, FFmpeg, encoder or RTMP process was restarted."
