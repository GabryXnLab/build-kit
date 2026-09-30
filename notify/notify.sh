#!/usr/bin/env bash
# Invocato da action.yml: gli input arrivano come variabili d'ambiente.
# Nessun token né identificativo qui dentro: arrivano tutti dal chiamante. I pulsanti
# con callback_data (Annulla, Rilancia, Errore…) li gestisce il bot che riceve i
# callback, se ne ha uno (ci-bot); per un bot qualsiasi restano pulsanti muti.
set -uo pipefail
shopt -s globstar nullglob

# Un repo pubblico senza i secret di Telegram, o un fork, arriva qui senza token:
# nessuna notifica e nessun avviso, perché non è un errore di chi compila.
if [ -z "$TG_TOKEN" ] || [ -z "$TG_CHAT" ]; then
  echo "Notifica Telegram saltata: token o chat non impostati."; exit 0
fi
# Il bot CI parla solo in un topic del supergruppo: mai in DM (chat positiva) né nel General.
case "$TG_CHAT" in
  -100*) ;;
  *) echo "::warning::Skip notifica Telegram: chat_id non è un supergruppo"; exit 0 ;;
esac
if [ -z "$TG_TOPIC" ]; then
  echo "::warning::Skip notifica Telegram: topic_id mancante (il General non si usa)"; exit 0
fi

html() { printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'; }

RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
case "$STATUS" in
  started) ICON="⏳" ;;
  success) ICON="✅" ;;
  cancelled) ICON="⚪" ;;
  *) ICON="❌" ;;
esac
HEAD="${ICON} <b>$(html "$TITLE")</b>"
META="<code>${GITHUB_SHA:0:7}</code> · $(html "${GITHUB_REF_NAME}") · $(html "${GITHUB_WORKFLOW}")"
[ "${GITHUB_RUN_ATTEMPT:-1}" -gt 1 ] && META="${META} · tentativo ${GITHUB_RUN_ATTEMPT}"
TEXT="${HEAD}"$'\n'"${META}"
[ "$STATUS" = "success" ] || [ "$STATUS" = "started" ] || TEXT="${TEXT}"$'\n'"Job <code>$(html "$GITHUB_JOB")</code>: ${STATUS}"
# Una riga sola: ci-bot la rilegge dal messaggio premuto per rilanciare con input cambiati.
if [ -n "$RUN_INPUTS" ] && [ "$RUN_INPUTS" != "null" ] && [ "$RUN_INPUTS" != "{}" ]; then
  RUN_INPUTS=$(printf '%s' "$RUN_INPUTS" | jq -c . 2>/dev/null || printf '%s' "$RUN_INPUTS" | tr -d '\r\n')
  TEXT="${TEXT}"$'\n'"⚙️ <code>$(html "$RUN_INPUTS")</code>"
else
  RUN_INPUTS=""
fi
[ -n "$DETAILS_FILE" ] && [ -f "$DETAILS_FILE" ] && DETAILS="${DETAILS:+$DETAILS$'\n'}$(cat "$DETAILS_FILE")"
[ -n "$DETAILS" ] && TEXT="${TEXT}"$'\n'"<pre>$(html "${DETAILS:0:2500}")</pre>"

# Il topic è un campo a parte: un message_thread_id vuoto viene rifiutato dalla Bot API.
DEST=(-F "chat_id=${TG_CHAT}" -F "parse_mode=HTML")
[ -n "$TG_TOPIC" ] && DEST+=(-F "message_thread_id=${TG_TOPIC}")

# L'URL contiene il token: a curl arriva da stdin (-K -), perché negli argomenti lo
# leggerebbe con ps qualunque utente della macchina del runner.
curl_url() { # $1=URL, poi le opzioni di curl
  local url=$1; shift
  printf 'url = "%s"\n' "$url" | curl "$@" -K -
}

# Un Local Bot API Server sul runner, se c'è, porta l'upload da 50 MB a ~2 GB.
TG_LOCAL="${TELEGRAM_API_BASE:-http://localhost:8081}"
if curl_url "${TG_LOCAL}/bot${TG_TOKEN}/getMe" -sf -m 5 -o /dev/null 2>/dev/null; then
  API="${TG_LOCAL}/bot${TG_TOKEN}"; MAX_BYTES=2097152000; MAX_LABEL="2 GB, server locale"
else
  API="https://api.telegram.org/bot${TG_TOKEN}"; MAX_BYTES=52428800; MAX_LABEL="50 MB, Bot API cloud"
fi

RESP="${RUNNER_TEMP:-/tmp}/tg_resp.json"
call() { # $1=metodo, poi i campi -F
  local method=$1; shift
  local code
  code=$(curl_url "${API}/${method}" -s -o "$RESP" -w "%{http_code}" "${DEST[@]}" "$@" || echo "000")
  if [ "$code" = "200" ]; then echo "Telegram ${method}: ok"; return 0; fi
  echo "warn: Telegram ${method} fallito (HTTP ${code})"; cat "$RESP" 2>/dev/null; echo
  return 1
}

# callback_data ha un limite di 64 byte: l'id numerico del repo invece di owner/nome.
R="${GITHUB_REPOSITORY_ID}:${GITHUB_RUN_ID}"

# Con MESSAGE_ID (dallo step con status started) l'esito riscrive quel messaggio invece di
# mandarne un secondo; se non si può più modificare, ne manda uno nuovo.
send_text() { # $1=testo, $2=tastiera JSON o vuoto (vuoto toglie quella del messaggio)
  local markup=()
  [ -n "$2" ] && markup=(--form-string "reply_markup=$2")
  if [ -n "$MESSAGE_ID" ] && call editMessageText -F "message_id=${MESSAGE_ID}" \
      --form-string "text=$1" ${markup[@]+"${markup[@]}"}; then
    return
  fi
  call sendMessage --form-string "text=$1" ${markup[@]+"${markup[@]}"}
}

if [ "$STATUS" = "started" ]; then
  KEYBOARD='{"inline_keyboard":[[{"text":"⏹ Annulla","callback_data":"cx:'"$R"'"},{"text":"🔎 Log","url":"'"$RUN_URL"'"}]]}'
  call sendMessage --form-string "text=${TEXT}"$'\n'"In corso…" --form-string "reply_markup=${KEYBOARD}"
  ID=$(grep -o '"message_id":[0-9]*' "$RESP" 2>/dev/null | head -1 | cut -d: -f2)
  [ -n "$ID" ] && [ -n "${GITHUB_OUTPUT:-}" ] && echo "message_id=${ID}" >> "$GITHUB_OUTPUT"
  exit 0
fi

if [ "$STATUS" != "success" ]; then
  # Il tentativo serve a errore e correzione AI: dopo un rilancio i job «latest» sono altri.
  A="${R}:${GITHUB_RUN_ATTEMPT:-1}"
  KEYBOARD='{"inline_keyboard":['
  KEYBOARD+='[{"text":"🔁 Rilancia","callback_data":"rr:'"$R"'"},{"text":"🔁 Solo falliti","callback_data":"rf:'"$R"'"}],'
  KEYBOARD+='[{"text":"🧾 Errore","callback_data":"lg:'"$A"'"},{"text":"🤖 Risolvi con AI","callback_data":"ai:'"$A"'"}],'
  LAST='{"text":"🔎 Log","url":"'"$RUN_URL"'"}'
  case "$RUN_INPUTS" in
    *'"clear_cache"'*) LAST='{"text":"🧹 Senza cache","callback_data":"cc:'"$R"'"},'"$LAST" ;;
  esac
  KEYBOARD+="[${LAST}]]}"
  send_text "$TEXT" "$KEYBOARD"
  # Un bot non riceve i propri messaggi: dove gira ci-bot (runner self-hosted) gli si lascia
  # l'id del messaggio, perché ci aggiunga la causa probabile. Altrove la cartella non c'è.
  INBOX="${HOME:-}/.local/state/ci-bot/inbox"
  ID=$(grep -o '"message_id":[0-9]*' "$RESP" 2>/dev/null | head -1 | cut -d: -f2)
  if [ "$STATUS" = "failure" ] && [ -n "$ID" ] && [ -d "$INBOX" ] && command -v jq >/dev/null; then
    F="${INBOX}/${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT:-1}-${ID}"
    if jq -n --arg chat_id "$TG_CHAT" --arg message_id "$ID" --arg repo_id "$GITHUB_REPOSITORY_ID" \
      --arg run_id "$GITHUB_RUN_ID" --arg attempt "${GITHUB_RUN_ATTEMPT:-1}" \
      --arg runner "${RUNNER_NAME:-}" --arg text "$TEXT" --argjson keyboard "$KEYBOARD" \
      '$ARGS.named' > "$F.tmp"; then
      mv "$F.tmp" "$F.json"
    else
      rm -f "$F.tmp"
    fi
  fi
  exit 0
fi

FOUND=()
while IFS= read -r pattern; do
  [ -n "$pattern" ] || continue
  for f in $pattern; do [ -f "$f" ] && FOUND+=("$f"); done
done <<< "$FILES"

TEXT="${TEXT}"$'\n'"<a href=\"${RUN_URL}\">Run</a>"
if [ "${#FOUND[@]}" -eq 0 ]; then
  [ -n "$FILES" ] && TEXT="${TEXT}"$'\n'"⚠️ Nessun artefatto trovato."
  send_text "$TEXT" ""
  exit 0
fi
[ -n "$MESSAGE_ID" ] && call editMessageText -F "message_id=${MESSAGE_ID}" \
  --form-string "text=${TEXT}"$'\n'"📦 Artefatti qui sotto."

# Per quando il file finisce sepolto nel topic o supera il limite: ci-bot lo rimanda dal run.
RESEND='{"inline_keyboard":[[{"text":"📦 Rimanda artefatti","callback_data":"ra:'"$R"'"}]]}'
for f in "${FOUND[@]}"; do
  BYTES=$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f" 2>/dev/null || echo 0)
  MB=$(( BYTES / 1024 / 1024 ))
  CAPTION="${TEXT}"$'\n'"📦 <code>$(html "$(basename "$f")")</code> (${MB} MB)"
  echo "Artefatto: $f (${MB} MB)"
  if [ "$BYTES" -gt 0 ] && [ "$BYTES" -le "$MAX_BYTES" ]; then
    call sendDocument -F "document=@${f}" --form-string "caption=${CAPTION}" \
      --form-string "reply_markup=${RESEND}"
  else
    call sendMessage --form-string "text=${CAPTION}"$'\n'"⚠️ Oltre il limite (${MAX_LABEL}): scaricalo dagli artifact del run." \
      --form-string "reply_markup=${RESEND}"
  fi
done
