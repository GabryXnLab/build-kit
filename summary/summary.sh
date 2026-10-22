#!/usr/bin/env bash
# Vedi CLAUDE.md, «Riassunto di cache». Ogni dato mancante è «n/d»: il riassunto non fa mai fallire il job.
set -uo pipefail

STACK=${STACK:-}
PROFILE=${PROFILE:-}
OUT=${GITHUB_OUTPUT:-/dev/null}
SUM=${GITHUB_STEP_SUMMARY:-/dev/null}
case "$(uname -m)" in aarch64|arm64) ARCH=arm64 ;; x86_64|amd64) ARCH=x64 ;; *) ARCH=$(uname -m) ;; esac
slug() { printf '%s' "$1" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9\n' '_' | sed 's/__*/_/g;s/^_//;s/_$//' | cut -c1-24; }

# Fasi: durata dei passi del job in corso (API), quelli sotto 5 s esclusi.
FASI=n/d; TOTALE=n/d
if [ -n "${GH_TOKEN:-}" ] && command -v jq >/dev/null 2>&1; then
  JOBS_JSON=$(mktemp)
  CODE=$(curl -sS --max-time 15 -o "$JOBS_JSON" -w '%{http_code}' -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" \
        "${GITHUB_API_URL:-https://api.github.com}/repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID/attempts/${GITHUB_RUN_ATTEMPT:-1}/jobs?per_page=100" 2>&1 || true)
  J=""
  if [ "$CODE" = 200 ]; then
    J=$(jq -c --arg r "${RUNNER_NAME:-}" '[.jobs[] | select(.runner_name == $r and .status == "in_progress")] | .[0] // empty' "$JOBS_JSON" 2>&1 || true)
  fi
  [ -n "$J" ] || echo "build-kit/summary: job non trovato (API HTTP $CODE), fasi non disponibili"
  rm -f "$JOBS_JSON"
  if [ -n "$J" ]; then
    FASI=$(printf '%s' "$J" | jq -r '.steps[] | select(.completed_at != null and .started_at != null)
        | [(.name), ((.completed_at | fromdateiso8601) - (.started_at | fromdateiso8601))] | @tsv' 2>/dev/null \
      | while IFS=$'\t' read -r n d; do
          [ "${d:-0}" -ge 5 ] 2>/dev/null || continue
          case "$n" in "Set up job"|"Post "*|"Complete job") continue ;; esac
          printf '%s:%ss,' "$(slug "$n")" "$d"
        done)
    FASI=${FASI%,}; FASI=${FASI:-n/d}
    TOTALE=$(printf '%s' "$J" | jq -r '(now - (.started_at | fromdateiso8601)) | floor' 2>/dev/null || echo n/d)
  fi
fi

# sccache: il server interrogato è quello del job (stesse variabili d'ambiente di cargo).
SCC=n/d; SCC_RAW=""
if [ -n "${RUSTC_WRAPPER:-}" ] && [ -x "${RUSTC_WRAPPER}" ]; then
  SCC_RAW=$("$RUSTC_WRAPPER" --show-stats 2>&1 || true)
  req=$(printf '%s\n' "$SCC_RAW" | awk '/^Compile requests[[:space:]]+[0-9]+$/ {print $NF; exit}')
  hit=$(printf '%s\n' "$SCC_RAW" | awk '/^Cache hits[[:space:]]+[0-9]+$/ {print $NF; exit}')
  miss=$(printf '%s\n' "$SCC_RAW" | awk '/^Cache misses[[:space:]]+[0-9]+$/ {print $NF; exit}')
  loc=$(printf '%s\n' "$SCC_RAW" | awk '/^Cache location/ {sub(/^Cache location[[:space:]]+/, ""); print $1; exit}' | tr -d ',' | tr 'A-Z' 'a-z')
  SCC="hit:${hit:-n/d},miss:${miss:-n/d},req:${req:-n/d},backend:${loc:-n/d}"
fi

# Gradle: una riga «ESITO :task» per task (init script gx-task-stats.gradle di build-kit/setup).
GR=n/d
F=${GX_TASK_STATS:-}
if [ -n "$F" ] && [ -s "$F" ]; then
  c() { grep -c "^$1 " "$F" || true; }
  fc=$(c FROM_CACHE); ut=$(c UP_TO_DATE); ns=$(c NO_SOURCE); sk=$(c SKIPPED); ex=$(c EXECUTED); fl=$(c FAILED)
  GR="cache:$fc,uptodate:$ut,eseguiti:$((ex + fl)),altri:$((ns + sk)),totale:$(wc -l < "$F" | tr -d ' ')"
fi

# s3-cache: gli eventi scritti da s3cache.sh (modo, tipo, esito, MB, secondi).
S3=n/d
EV="${GX_S3_STATE_DIR:-${RUNNER_TEMP:-/tmp}/gx-s3-cache}/events.tsv"
if [ -s "$EV" ]; then
  S3=$(awk -F'\t' '{printf "%s%s:%s:%sMB:%ss", (NR>1?",":""), ($1=="save"?"save-":"") $2, $3, $4, $5}' "$EV")
fi

LINE="runner=${RUNNER_NAME:-n/d} profile=${PROFILE:-n/d} arch=$ARCH stack=${STACK:-n/d} totale=${TOTALE}s fasi=$FASI sccache=$SCC gradle=$GR s3=$S3"
echo "line=$LINE" >> "$OUT"
echo "cache_summary: $LINE"

{
  echo "### Cache e tempi"
  echo "| Runner | Profilo | Arch | Totale | sccache | Gradle | s3-cache |"
  echo "| --- | --- | --- | --- | --- | --- | --- |"
  echo "| ${RUNNER_NAME:-n/d} | ${PROFILE:-n/d} | $ARCH | ${TOTALE}s | $SCC | $GR | $S3 |"
  echo
  echo "Fasi: \`$FASI\`"
  echo
  echo "\`$LINE\`"
} >> "$SUM"

if [ -n "$SCC_RAW" ]; then
  echo "::group::sccache --show-stats"
  printf '%s\n' "$SCC_RAW" | head -40
  pgrep -a sccache 2>/dev/null || echo "(nessun server sccache vivo)"
  env | grep -E '^(SCCACHE_(BUCKET|DIR|S3_KEY_PREFIX|SERVER_PORT|RECACHE|CACHE_SIZE)|RUSTC_WRAPPER|CARGO_BUILD_RUSTC_WRAPPER|CARGO_TARGET_DIR)=' || true
  echo "::endgroup::"
fi
exit 0
