#!/usr/bin/env bash
# Archivi di dipendenze sul bucket S3 della cache remota (docs/cache-contract.md, prefisso v1/deps/).
# Modalità restore (inizio job) e save (fine job). Mai un errore per la build: ogni problema è un avviso
# e la cache è un miss.
set -euo pipefail

MODE=${MODE:?}
STACK=${STACK:?}
ITEMS=${ITEMS:-}
CACHE_ENV=${CACHE_ENV:-}
CLEAR_CACHE=${CLEAR_CACHE:-false}
MAX_MB=${MAX_MB:-1500}
STATE_DIR=${GX_S3_STATE_DIR:-${RUNNER_TEMP:-/tmp}/gx-s3-cache}
S5CMD_VERSION=2.3.0
S5CMD_SHA256_X64=de0fdbfa3aceae55e069ba81a0fc17b2026567637603734a387b2fca06c299b4
S5CMD_SHA256_ARM64=1439f0d00ecedcd2a2f1f2c6749bbb0152b2257bf5086f29646ec8ae38798e24

warn() { echo "::warning::s3-cache: $*"; }
note() { echo "s3-cache: $*"; }
# Una riga per archivio (modo, tipo, esito, MB, secondi) che build-kit/summary legge a fine job.
ev() { printf '%s\t%s\t%s\t%s\t%s\n' "$MODE" "$1" "$2" "$3" "$4" >> "$STATE_DIR/events.tsv"; }

# Repo pubblici e fork mai: la chiave di scrittura non deve arrivare a codice di terzi.
if [ "$(uname -s)" != "Linux" ]; then note "solo Linux, si salta"; exit 0; fi
if [ "${REPO_PRIVATE:-}" != "true" ] || [ "${IS_FORK:-}" = "true" ]; then
  note "repo pubblico o fork, si salta"; exit 0
fi

# Come in setup: si leggono solo le chiavi note, senza source.
ENV_TEXT=$CACHE_ENV
if [ -z "$ENV_TEXT" ]; then
  for f in "$HOME/secrets/oci-build-cache.env" /var/lib/ci/secrets/oci-build-cache.env; do
    if [ -r "$f" ]; then ENV_TEXT=$(cat "$f"); break; fi
  done
fi
C_AWS_ACCESS_KEY_ID="" C_AWS_SECRET_ACCESS_KEY="" C_AWS_REGION="" C_S3_ENDPOINT="" C_S3_BUCKET=""
while IFS= read -r line || [ -n "$line" ]; do
  line=${line%$'\r'}
  case "$line" in
    AWS_ACCESS_KEY_ID=*|AWS_SECRET_ACCESS_KEY=*|AWS_REGION=*|S3_ENDPOINT=*|S3_BUCKET=*)
      v=${line#*=}; v=${v#[\"\']}; v=${v%[\"\']}
      printf -v "C_${line%%=*}" '%s' "$v" ;;
  esac
done <<< "$ENV_TEXT"
for v in "$C_AWS_ACCESS_KEY_ID" "$C_AWS_SECRET_ACCESS_KEY" "$C_S3_ENDPOINT"; do
  if [ -n "$v" ]; then echo "::add-mask::$v"; fi
done
if [ -z "$C_AWS_ACCESS_KEY_ID" ] || [ -z "$C_AWS_SECRET_ACCESS_KEY" ] || [ -z "$C_AWS_REGION" ] \
   || [ -z "$C_S3_ENDPOINT" ] || [ -z "$C_S3_BUCKET" ]; then
  warn "credenziali assenti, cache delle dipendenze inerte"; exit 0
fi
export AWS_ACCESS_KEY_ID=$C_AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=$C_AWS_SECRET_ACCESS_KEY AWS_REGION=$C_AWS_REGION
BUCKET=$C_S3_BUCKET

case "$(uname -m)" in aarch64|arm64) ARCH=arm64 ;; *) ARCH=x64 ;; esac
OSARCH=linux-$ARCH

TOOLS="$HOME/ci/tools/bin"
S5=$(command -v s5cmd || true)
if [ -z "$S5" ]; then
  tmp=$(mktemp -d)
  if [ "$ARCH" = arm64 ]; then pkg=Linux-arm64; sha=$S5CMD_SHA256_ARM64; else pkg=Linux-64bit; sha=$S5CMD_SHA256_X64; fi
  if curl -fsSL --retry 3 -o "$tmp/s.tgz" "https://github.com/peak/s5cmd/releases/download/v$S5CMD_VERSION/s5cmd_${S5CMD_VERSION}_$pkg.tar.gz" \
     && echo "$sha  $tmp/s.tgz" | sha256sum -c - >/dev/null \
     && tar -xzf "$tmp/s.tgz" -C "$tmp" s5cmd; then
    mkdir -p "$TOOLS"; install -m 755 "$tmp/s5cmd" "$TOOLS/s5cmd"; S5=$TOOLS/s5cmd
  else
    rm -rf "$tmp"; warn "s5cmd non scaricabile o sha256 diverso, cache inerte"; exit 0
  fi
  rm -rf "$tmp"
fi
if ! command -v zstd >/dev/null 2>&1; then warn "zstd assente, cache inerte"; exit 0; fi

s3() { "$S5" --endpoint-url "$C_S3_ENDPOINT" --retry-count 3 "$@"; }

slug() { printf '%s' "$1" | tr -c 'A-Za-z0-9._\n-' '-' ; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Percorsi dell'item relativi a $HOME (~/x, $HOME/x o x).
rel_paths() {
  local p out=""
  for p in $1; do
    p=${p/#\~\//$HOME/}; p=${p/#\$HOME\//$HOME/}
    out="$out ${p#"$HOME"/}"
  done
  printf '%s' "${out# }"
}

hash_of() { # $1 = glob separati da virgola, $2 = versione strumento
  local globs=$1 tool=$2 files="" g spec
  local -a specs=()
  IFS=',' read -r -a specs <<< "$globs"
  for g in "${specs[@]}"; do
    g=${g// /}
    [ -n "$g" ] || continue
    files+=$(git -C "${GITHUB_WORKSPACE:-.}" ls-files -- ":(glob)$g" 2>/dev/null || true)$'\n'
  done
  files=$(printf '%s' "$files" | sed '/^$/d' | sort -u)
  [ -n "$files" ] || return 1
  { printf '%s\n' "$tool"; while IFS= read -r f; do cat "${GITHUB_WORKSPACE:-.}/$f"; done <<< "$files"; } | sha256sum | cut -c1-16
}

touch_index() { # $1 = chiave completa dell'oggetto
  local idx="v1/index/${1#v1/}.json" f
  f=$(mktemp)
  printf '{"last_used": "%s", "run": "%s"}\n' "$(now)" "${GITHUB_RUN_ID:-0}" > "$f"
  s3 cp "$f" "s3://$BUCKET/$idx" >/dev/null 2>&1 || warn "indice non aggiornato ($idx)"
  rm -f "$f"
}

restore_item() { # tipo|percorsi|glob|versione
  local tipo=$1 paths=$2 globs=$3 tool=$4 h name prefix key status=miss found t0 f size
  if ! h=$(hash_of "$globs" "$tool"); then
    warn "$tipo: nessun lockfile ($globs), item saltato"; return 0
  fi
  name="$(slug "$tool")-$h.tar.zst"
  prefix="v1/deps/$STACK/$tipo/$OSARCH/"
  key=$prefix$name
  if [ "$CLEAR_CACHE" = "true" ]; then
    note "$tipo: clear_cache, niente ripristino"
    printf '%s\t%s\t%s\t%s\t%s\n' "$tipo" skip "$key" "$paths" "$tool" >> "$STATE"; return 0
  fi
  found=""
  if s3 ls "s3://$BUCKET/$key" >/dev/null 2>&1; then
    status=exact; found=$key
  else
    # Ripiego: stesso strumento e versione, lockfile diverso; il più recente.
    f=$(s3 ls "s3://$BUCKET/${prefix}$(slug "$tool")-*" 2>/dev/null | sort -k1,2 | tail -1 | awk '{print $4}' || true)
    if [ -n "$f" ]; then status=fallback; found=$prefix$f; fi
  fi
  if [ -n "$found" ]; then
    t0=$SECONDS
    f=$(mktemp)
    if s3 cp "s3://$BUCKET/$found" "$f" >/dev/null 2>&1 \
       && zstd -dcq "$f" | tar -xf - -C "$HOME"; then
      size=$(( $(stat -c %s "$f") / 1048576 ))
      note "$tipo: $status ${found##*/} (${size} MB, $((SECONDS - t0)) s)"
      ev "$tipo" "$status" "$size" "$((SECONDS - t0))"
      touch_index "$found"
    else
      warn "$tipo: archivio $found illeggibile, si ricostruisce"
      status=miss
    fi
    rm -f "$f"
  else
    note "$tipo: miss ($name)"
    ev "$tipo" miss 0 0
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$tipo" "$status" "$key" "$paths" "$tool" >> "$STATE"
}

save_item() { # riga di stato
  local tipo status key paths tool rels p t0 f size
  IFS=$'\t' read -r tipo status key paths tool <<< "$1"
  if [ "$status" = exact ]; then note "$tipo: hit esatto, niente da salvare"; return 0; fi
  # Un percorso mancante (es. ~/.cargo/git senza dipendenze git) non impedisce di salvare gli altri.
  rels=""
  for p in $(rel_paths "$paths"); do
    if [ -e "$HOME/$p" ]; then rels="$rels $p"; fi
  done
  if [ -z "$rels" ]; then
    note "$tipo: percorsi assenti ($paths), niente da salvare"; return 0
  fi
  t0=$SECONDS
  f=$(mktemp)
  # shellcheck disable=SC2086
  if ! tar -cf - -C "$HOME" $rels 2>/dev/null | zstd -T0 -3 -q -o "$f" -f; then
    warn "$tipo: archivio non creato"; rm -f "$f"; return 0
  fi
  size=$(( $(stat -c %s "$f") / 1048576 ))
  if [ "$size" -gt "$MAX_MB" ]; then
    warn "$tipo: archivio di ${size} MB oltre il limite di ${MAX_MB} MB, non salvato"; rm -f "$f"; return 0
  fi
  if s3 cp --metadata "gx-schema=1" --metadata "gx-repo=${GITHUB_REPOSITORY:-}" --metadata "gx-stack=$STACK" \
       --metadata "gx-tool-version=$tool" --metadata "gx-arch=$ARCH" --metadata "gx-run=${GITHUB_RUN_ID:-0}" \
       "$f" "s3://$BUCKET/$key" >/dev/null 2>&1; then
    note "$tipo: salvato ${key##*/} (${size} MB, $((SECONDS - t0)) s)"
    ev "$tipo" salvato "$size" "$((SECONDS - t0))"
    touch_index "$key"
  else
    warn "$tipo: upload di $key fallito"
  fi
  rm -f "$f"
}

mkdir -p "$STATE_DIR"
STATE="$STATE_DIR/$STACK.tsv"
case "$MODE" in
  restore)
    : > "$STATE"
    while IFS='|' read -r tipo paths globs tool; do
      tipo=${tipo// /}
      case "$tipo" in ''|'#'*) continue ;; esac
      restore_item "$tipo" "$paths" "$globs" "$tool" || warn "$tipo: ripristino fallito"
    done <<< "$ITEMS" ;;
  save)
    if [ ! -s "$STATE" ]; then note "nessun ripristino precedente, niente da salvare"; exit 0; fi
    while IFS= read -r line; do
      save_item "$line" || warn "salvataggio fallito"
    done < "$STATE" ;;
  *) echo "::error::mode non valido: $MODE"; exit 1 ;;
esac
