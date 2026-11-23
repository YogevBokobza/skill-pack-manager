#!/usr/bin/env sh
# Adds "Skill Pack Manager" to the application menu (freedesktop .desktop entry).
# Run from the repository folder:  sh install-linux.sh
set -eu
root=$(cd "$(dirname "$0")" && pwd)
python=$(command -v python3)
dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$dir"
cat > "$dir/skill-pack-manager.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Skill Pack Manager
Comment=Choose which agent skills and MCP servers each project gets
Exec=sh -c 'cd "$root" && "$python" -m spm'
Icon=$root/spm/static/icon.png
Terminal=false
Categories=Development;
EOF
echo "created $dir/skill-pack-manager.desktop"
