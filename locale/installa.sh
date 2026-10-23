#!/usr/bin/env bash
# Build Rust a mano e degli agenti su una macchina self-hosted (nexus-core): sccache con la cache S3 della CI
# e un limite automatico allo spazio delle target. Idempotente: si rilancia dopo ogni pull di build-kit.
#
#   locale/installa.sh            # installa o aggiorna
#   locale/installa.sh --verifica # stato, senza cambiare nulla
#
# Cosa fa:
#   ~/.local/bin/gx-sccache        → symlink a locale/gx-sccache (rustc-wrapper)
#   ~/.local/bin/gx-cc-lld         → symlink a locale/gx-cc-lld (linker: lld del toolchain, fuori dalla CI)
#   ~/.cargo/config.toml           → build.rustc-wrapper, build.incremental = false e linker dell'host (file generato)
#   ~/ci/tools/bin/sccache         → stessa versione e sha256 di setup/action.yml, se manca
#   timer di utente gx-pulisci-target (ogni 10 minuti) → locale/pulisci-target.py
#                                    e locale/sorveglia-sccache.py (riavvia il server 4227 se bloccato)
set -euo pipefail
kit=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
bin=$HOME/.local/bin
cfg=${CARGO_HOME:-$HOME/.cargo}/config.toml
units=$HOME/.config/systemd/user
mark="# Generato da build-kit/locale/installa.sh"

if [ "${1:-}" = --verifica ]; then
  ls -l "$bin/gx-sccache"; cat "$cfg"
  systemctl --user list-timers gx-pulisci-target.timer --no-pager
  "$bin/gx-sccache" --show-stats | grep -E 'Cache location|Compile requests |Cache hits  |Cache misses  '
  df -h /
  exit 0
fi

mkdir -p "$bin" "$units" "$(dirname "$cfg")"
ln -sfn "$kit/gx-sccache" "$bin/gx-sccache"
ln -sfn "$kit/gx-cc-lld" "$bin/gx-cc-lld"

if [ -e "$cfg" ] && ! grep -qF "$mark" "$cfg"; then
  echo "$cfg esiste e non è generato da qui: aggiungi a mano [build] rustc-wrapper = \"$bin/gx-sccache\" e incremental = false" >&2
  exit 1
fi
cat > "$cfg" <<EOF
$mark: non modificarlo a mano.
# Ogni cargo dell'utente (a mano, agenti, worktree) passa da sccache con la cache S3 della CI; nei job CI
# il RUSTC_WRAPPER di setup prevale e gx-sccache resta inerte. Incrementale spento: sccache non mette in
# cache i crate incrementali e le cartelle incremental/ erano il grosso del disco (1-2 GB per crate e worktree).
# Per riaverlo in una sessione: CARGO_INCREMENTAL=1 (vince sulla config).
[build]
rustc-wrapper = "$bin/gx-sccache"
incremental = false

# Link con l'lld del toolchain (gx-cc-lld); un progetto può sovrascriverlo nel suo .cargo/config.toml.
[target.$(uname -m)-unknown-linux-gnu]
linker = "$bin/gx-cc-lld"
EOF

sc=$HOME/ci/tools/bin/sccache
if [ ! -x "$sc" ]; then
  act=$kit/../setup/action.yml
  ver=$(sed -n "s/^ *SCCACHE_VERSION: '\(.*\)'/\1/p" "$act")
  case "$(uname -m)" in
    aarch64|arm64) arch=aarch64; sha=$(sed -n "s/^ *SCCACHE_SHA256_AARCH64: '\(.*\)'/\1/p" "$act") ;;
    *) arch=x86_64; sha=$(sed -n "s/^ *SCCACHE_SHA256_X86_64: '\(.*\)'/\1/p" "$act") ;;
  esac
  name=sccache-v$ver-$arch-unknown-linux-musl
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/s.tgz" "https://github.com/mozilla/sccache/releases/download/v$ver/$name.tar.gz"
  echo "$sha  $tmp/s.tgz" | sha256sum -c -
  tar -xzf "$tmp/s.tgz" -C "$tmp"
  mkdir -p "$(dirname "$sc")"
  install -m 755 "$tmp/$name/sccache" "$sc"
  rm -rf "$tmp"
fi

cat > "$units/gx-pulisci-target.service" <<EOF
$mark
[Unit]
Description=Pulizia delle target cargo di build a mano e agenti (build-kit/locale)

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $kit/sorveglia-sccache.py
ExecStart=/usr/bin/python3 $kit/pulisci-target.py --attesa 540
Nice=19
IOSchedulingClass=idle
EOF
cat > "$units/gx-pulisci-target.timer" <<EOF
$mark
[Unit]
Description=Pulizia delle target cargo ogni 10 minuti

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now gx-pulisci-target.timer >/dev/null
echo "installato: $bin/gx-sccache, $cfg, timer gx-pulisci-target"
