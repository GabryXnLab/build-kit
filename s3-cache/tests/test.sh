#!/usr/bin/env bash
# Test offline di s3cache.sh con un finto s5cmd. Uso: bash s3-cache/tests/test.sh
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPT=$HERE/../s3cache.sh
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
export FAKE_S3=$T/s3 HOME=$T/home GITHUB_WORKSPACE=$T/ws GX_S3_STATE_DIR=$T/state
export GITHUB_REPOSITORY=org/repo GITHUB_RUN_ID=42 REPO_PRIVATE=true
mkdir -p "$FAKE_S3/bk" "$HOME" "$GITHUB_WORKSPACE" "$T/bin"
ln -s "$HERE/fake-s5cmd" "$T/bin/s5cmd"
export PATH=$T/bin:$PATH
CREDS=$'AWS_ACCESS_KEY_ID=AK\nAWS_SECRET_ACCESS_KEY=SK\nAWS_REGION=eu-x\nS3_ENDPOINT=https://s3.invalid\nS3_BUCKET=bk\n'
ITEMS='pub-cache|~/.pub-cache|**/pubspec.lock|flutter-3.35.0'
fails=0
ok() { echo "ok   - $1"; }
ko() { echo "FAIL - $1"; fails=$((fails + 1)); }
check() { if eval "$2"; then ok "$1"; else ko "$1"; echo "$out" | sed "s/^/    | /"; fi; }
run() { MODE=$1 STACK=flutter ITEMS="$ITEMS" CACHE_ENV="${CREDS_IN-$CREDS}" CLEAR_CACHE="${CLEAR_CACHE:-false}" bash "$SCRIPT" 2>&1; }

( cd "$GITHUB_WORKSPACE" && git init -q && echo 'deps: v1' > pubspec.lock && git add . )
mkdir -p "$HOME/.pub-cache/hosted" && echo pkg > "$HOME/.pub-cache/hosted/a.txt"
case "$(uname -m)" in aarch64|arm64) OA=linux-arm64 ;; *) OA=linux-x64 ;; esac
D=$FAKE_S3/bk/v1/deps/flutter/pub-cache/$OA

out=$(run restore); check "miss al primo giro" '[[ $out == *"miss"* ]]'
out=$(run save); check "save crea l'archivio" '[ "$(ls $D/*.tar.zst | wc -l)" = 1 ]'
f=$(ls "$D"/*.tar.zst)
check "chiave con versione strumento" '[[ $f == */flutter-3.35.0-????????????????.tar.zst ]]'
check "metadati gx-*" 'grep -q gx-schema=1 "$f.meta" && grep -q gx-repo=org/repo "$f.meta" && grep -q gx-stack=flutter "$f.meta" && grep -q gx-tool-version=flutter-3.35.0 "$f.meta" && grep -q gx-run=42 "$f.meta"'
idx=$FAKE_S3/bk/v1/index/deps/flutter/pub-cache/$OA/$(basename "$f").json
check "indice scritto" 'grep -q "\"last_used\"" "$idx"'

rm -rf "$HOME/.pub-cache" "$idx"
out=$(run restore); check "hit esatto" '[[ $out == *"exact"* ]] && [ "$(cat $HOME/.pub-cache/hosted/a.txt)" = pkg ]'
check "indice aggiornato a ogni hit" '[ -f "$idx" ]'
before=$(stat -c %Y "$f")
out=$(run save); check "niente save su hit esatto" '[[ $out == *"hit esatto"* ]] && [ "$(stat -c %Y "$f")" = "$before" ]'

( cd "$GITHUB_WORKSPACE" && echo 'deps: v2' > pubspec.lock )
rm -rf "$HOME/.pub-cache"
out=$(run restore); check "ripiego per prefisso" '[[ $out == *"fallback"* ]] && [ -f "$HOME/.pub-cache/hosted/a.txt" ]'
echo more > "$HOME/.pub-cache/hosted/b.txt"
out=$(run save); check "save dopo ripiego: seconda chiave" '[ "$(ls $D/*.tar.zst | wc -l)" = 2 ]'

ITEMS='pub-cache|~/.pub-cache ~/.pub-git|**/pubspec.lock|flutter-7.7'; out=$(run restore); out=$(run save); check "un percorso mancante non impedisce il save" '[ "$(ls $D/flutter-7.7-*.tar.zst | wc -l)" = 1 ]'
ITEMS='pub-cache|~/.pub-cache|**/pubspec.lock|flutter-3.35.0'
rm -rf "$HOME/.pub-cache"
out=$(ITEMS='pub-cache|~/.pub-cache|**/pubspec.lock|flutter-9.9' run restore); check "altra versione dello strumento: miss" '[[ $out == *"miss"* ]]'

mkdir -p "$HOME/.pub-cache" && echo local > "$HOME/.pub-cache/l.txt"
out=$(run restore); check "percorsi popolati: niente download" '[[ $out == *"già popolati"* && $(cut -f3 "$GX_S3_STATE_DIR/events.tsv" | tail -1) == *locale* ]]'
echo 'deps: v3' > "$GITHUB_WORKSPACE/pubspec.lock"; ( cd "$GITHUB_WORKSPACE" && git add . )
out=$(run restore); out2=$(run save); check "percorsi popolati e chiave assente: il save li archivia" '[ "$(ls $D/flutter-3.35.0-*.tar.zst | wc -l)" = 3 ]'
out=$(run restore); out2=$(run save); check "percorsi popolati e hit esatto: nessun nuovo save" '[[ $out == *"(exact)"* && $out2 == *"hit esatto"* ]]'
rm -rf "$HOME/.pub-cache"

out=$(CREDS_IN="" HOME=$T/home2 run restore); check "senza credenziali: avviso e successo" '[[ $out == *"credenziali assenti"* ]]'
out=$(REPO_PRIVATE=false run restore); check "repo pubblico: salta" '[[ $out == *"repo pubblico"* ]]'
out=$(IS_FORK=true run restore); check "fork: salta" '[[ $out == *"fork"* ]]'

rm -rf "$HOME/.pub-cache"
export CLEAR_CACHE=true; out=$(run restore); unset CLEAR_CACHE; check "clear_cache: niente ripristino" '[[ $out == *"clear_cache"* ]] && [ ! -e $HOME/.pub-cache ]'
out=$(run save); check "clear_cache: nessun percorso, nessun errore" '[[ $out != *"::error"* ]]'

ITEMS='pnpm-store|~/.pnpm|**/pnpm-lock.yaml|pnpm-9'; out=$(run restore); check "senza lockfile: item saltato" '[[ $out == *"nessun lockfile"* ]]'

[ "$fails" = 0 ] && echo "tutti i test ok" || { echo "$fails falliti"; exit 1; }
