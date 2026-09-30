# CLAUDE.md — build-kit

## Cos'è

I **componenti comuni delle build** di tutti i progetti dell'organizzazione: ciò che
`flutter-ci`, `expo-ci` e `desktop-ci` facevano ognuno a modo suo (quanti worker, quali
cache, cosa vuol dire «pulire») sta qui una volta sola, come azioni composite, più lo
script che lancia più build insieme. È il posto da cui parte chi scrive o rivede un
workflow di build: la sezione «Come si scrive una build» è il modello da seguire.

Contiene anche il **sistema di pubblicazione** comune: un repo privato di sviluppo che
porta `main` in uno specchio pubblico come PR di un commit solo, con uno scanner anti-fuga,
e i template per build e release dello specchio (sezione «Pubblicare un progetto»).

`README.md` ha l'uso; questo file i vincoli e il perché.

## Stack

Azioni composite di GitHub Actions (bash con `set -euo pipefail`), uno script bash, un
reusable workflow e uno script `python3` di sola libreria standard. Nessuna dipendenza,
niente da compilare.

## Struttura

```text
setup/action.yml     worker (max_workers), cache condivise della macchina, clear_cache di sola esecuzione
x86-64/action.yml    lanciatore per binari x86-64 sul runner ARM64: Box64 (default) o QEMU 10
notify/              esito su Telegram (action.yml + notify.sh): copia pubblica di ci-bot/notify
bin/ci-batch         lancia le build di più progetti in un colpo (gh workflow run)
public-sync/         specchio pubblico: sync.py (piano, commit, scanner), patterns generici,
                     test_sync.py, action.yml che dà al job la cartella dello script
.github/workflows/public-sync.yml   il reusable del sync: fetch, push, PR, issue, notifica
templates/publish/   da copiare in un progetto: wrapper del sync, exclude/patterns/allow
                     d'esempio, build e release dello specchio per flutter/, desktop/, expo/
```

## Comandi

```bash
python3 -c "import yaml,glob; [yaml.safe_load(open(f)) for f in glob.glob('*/action.yml')]"
bash -n bin/ci-batch && bin/ci-batch --list
bash -n notify/notify.sh && shellcheck notify/notify.sh
python3 -m py_compile public-sync/sync.py && python3 -B -m unittest public-sync/test_sync.py
actionlint .github/workflows/*.yml   # i template: copiati in un repo finto, placeholder sostituiti
```

## Architettura delle build

```text
progetto (thin wrapper: solo scelte)          es. kagami/.github/workflows/build-android.yml
  └─ uses: GabryXnLab/<stack>-ci/…@main        logica dello stack: flutter-ci | expo-ci | desktop-ci
       ├─ uses: GabryXnLab/build-kit/setup@main   worker, cache condivise, clear_cache
       ├─ uses: GabryXnLab/build-kit/x86-64@main  solo se serve un binario x86-64
       └─ uses: GabryXnLab/build-kit/notify@main  inizio ed esito su Telegram (tutti e tre)
```

Runner: `[self-hosted, nexus-core]` — **due istanze** sulla stessa macchina
(`~/ci/actions-runner` e `~/ci/actions-runner-2`, stesso utente), quindi due build in
parallelo e cache della macchina in comune. `runner: github` resta l'eccezione.

## Note architetturali importanti

- **`@main` è live per tutti**: i tre reusable usano `build-kit/…@main`. Input nuovi con
  default, mai rinominati né tolti senza aggiornare i chiamanti.
- **Il repo è pubblico, e deve restarlo.** `flutter-ci` è chiamato anche da repo
  pubblici, e un repo pubblico non può usare azioni di repo privati: GitHub le risolve
  tutte all'avvio del job, anche quelle di uno step con un `if` falso. Per questo
  `notify` è una copia di `ci-bot/notify` (che resta privato insieme al bot) e nessuna
  azione contiene token, chat o chiavi: arrivano dal chiamante, e senza la notifica si
  salta in silenzio. I tre reusable usano tutti `build-kit/notify`; `ci-bot/notify` resta
  per i workflow privati che lo chiamano direttamente, e una modifica funzionale di qui si
  porta là (differenze volute: testi, commenti e l'URL con il token passato a curl da
  stdin, perché negli argomenti si legge con `ps`). Lo stesso vale per ogni `uses:` dei
  reusable: solo azioni pubbliche, di qui o di terzi.
  Le azioni devono funzionare anche sui runner di GitHub (Linux e macOS): lì `setup`
  scrive solo variabili d'ambiente, `x86-64` non serve (host x86-64).
- **Su `runner: github` la cache è quella di GitHub, nel reusable.** Un runner GitHub è
  nuovo a ogni job e le cartelle della macchina non ci sono: ciò che sul self-hosted sta in
  `~/.gradle`, pub-cache, store di pnpm, sccache lo riprende la cache di GitHub, che è per
  repo (vale fra i run dello stesso progetto, non fra progetti). Sta nel reusable e non in
  `setup` perché percorsi e chiavi sono dello stack: flutter-ci `flutter-action` con
  `cache: true` (SDK e pub cache) e `gradle/actions/setup-gradle`; expo-ci lo store di pnpm
  nei job EAS; desktop-ci `Swatinem/rust-cache` e lo store di pnpm o la cache di npm. Gli
  step hanno `if` sul runner GitHub: sul self-hosted non girano, perché caricherebbero
  gigabyte della macchina nella cache di GitHub. Con `clear_cache` la cache dei risultati
  (cargo) non si riprende; le dipendenze scaricate sì, come sul self-hosted.
- **Sul self-hosted si va veloci facendo meno lavoro.** Misure su Kagami (APK release,
  26/09): 545 s, di cui 321 di `gen_snapshot` sotto QEMU, 51 di compilazione Dart, il resto
  Gradle quasi tutto dalla build cache. Da qui le regole sotto, in ordine di peso.
- **Un binario x86-64 si esegue con Box64, verificato, e QEMU 10 di riserva.** Box64
  traduce il codice e usa la libc nativa: `gen_snapshot` di Flutter 46 s contro 316 s di
  QEMU, `hermesc` di React Native 43 s contro 95 s, output **identico byte per byte**
  (confrontato su due `app.dill` di Kagami e su un bundle JS da 9 MB). Ma Box64 ha corse
  fra thread che emergono solo con la CPU contesa: `gen_snapshot` con i thread paralleli
  del GC del Dart VM cadeva in «double free or corruption» in CI (dove Gradle compila in
  parallelo all'AOT) e mai a mano a macchina libera. Riprodotto con quattro processi che
  occupano la CPU: 4 crash su 4, anche con `BOX64_DYNAREC_STRONGMEM`; 0 su 3 con il GC a
  un thread. Il master di Box64 cadeva anche senza carico. Regole che ne derivano:
  - solo una **release fissata** (v0.4.4), mai il master;
  - un programma multithread gira con meno thread interni possibile (`gen_snapshot`:
    `--marker_tasks=1 --scavenger_tasks=1 --no-concurrent_mark --no-concurrent_sweep`,
    che con `--deterministic` non cambiano l'output);
  - un emulatore si prova **sotto carico**, non a macchina libera: lì il difetto non si
    vede (bastano quattro `while :; do :; done` in background);
  - il lanciatore di `x86-64` ripete con QEMU se Box64 esce con errore;
  - dove un output sbagliato finirebbe su un telefono (`gen_snapshot`), **due esecuzioni
    in parallelo con ambienti di dimensione diversa**, accettate solo se riescono
    entrambe e coincidono (wrapper di flutter-ci); costa una CPU in più per ~46 s;
  - `hermesc` passa col lanciatore semplice: provato identico a QEMU 10 anche sotto
    carico (attenzione nei confronti: incorpora il percorso del sorgente, quindi si
    confronta solo a parità di percorso).
  `emulator: qemu` salta Box64 del tutto. Se Box64 manca o è di un altro commit, l'azione
  lo compila (~6 min) sotto `~/ci/tools`.
- **Le cache condivise non si cancellano da una build.** `~/.gradle` (dipendenze,
  trasformazioni e build cache di tutti i progetti Gradle), `~/.pub-cache`, lo store di
  pnpm, `~/.cache/ccache` e `~/.cache/sccache` servono a tutti: `clear_cache` le rende
  **non fidate per quel run** (build cache di Gradle spenta, ccache/sccache in
  `RECACHE`: ricompilano e riscrivono) e cancella solo le cartelle del progetto. Un tempo
  expo-ci cancellava `~/.gradle/caches` e ccache: una build «pulita» di un progetto
  costava la build successiva a tutti gli altri.
- **Progetti simili condividono la cache perché usano le stesse versioni.** La build
  cache di Gradle e `modules-2` si riusano fra progetti diversi solo a parità di Gradle,
  AGP e Kotlin (oggi Kagami e Riftgate: Gradle 9.3.1). ccache usa `CCACHE_BASEDIR=$HOME`,
  quindi un oggetto compilato nel checkout di un runner vale per l'altro runner e per un
  altro progetto con la stessa dipendenza. sccache fa lo stesso per le dipendenze Rust
  (tutte da `~/.cargo/registry`). Allineare le versioni fra progetti è quindi anche una
  scelta di velocità.
- **Gli intermedi del progetto restano fra i run.** Checkout con `clean: false` sul
  self-hosted (un runner GitHub è nuovo a ogni job); ciò che la pulizia di un altro
  workflow potrebbe togliere va fuori dal checkout: la cartella target di cargo sta in
  `~/ci/cache/cargo-target/<repo>`, perché mobile e desktop dello stesso repo condividono
  la cartella di lavoro e il checkout dell'uno cancellava gli intermedi dell'altro.
- **I worker li decide `max_workers`, per run.** `auto` prende le CPU libere al momento
  (4 meno il carico, mai sotto 2): da sola una build usa tutta la macchina, con un'altra
  build o un agente al lavoro si stringe. Passa per `GRADLE_OPTS` con `-D`, che prevale su
  `~/.gradle/gradle.properties` della macchina (lì restano 2 worker e 4 GB di heap per
  chi lancia Gradle a mano), e per `CARGO_BUILD_JOBS` e `CMAKE_BUILD_PARALLEL_LEVEL`.
- **Il daemon di Gradle resta spento.** Il runner uccide i processi rimasti a fine job,
  e un daemon vivo occuperebbe gigabyte a riposo su una macchina condivisa; l'avvio della
  JVM costa pochi secondi contro minuti di AOT.
- **Due runner, non di più.** 4 CPU e 23 GB: due build Gradle insieme stanno in memoria
  (4 GB di heap ciascuna più Kotlin), tre no. Il secondo runner non ha le etichette
  `ubuntu-latest`/`ubuntu-24.04` che il primo ha (e che gli fanno prendere anche job
  destinati a GitHub).

## Come si scrive una build (modello per progetti nuovi)

1. **Il progetto ha un thin wrapper** in `.github/workflows/`: solo `workflow_dispatch`,
   input `choice`/`boolean`, `run-name` con le scelte, `uses: GabryXnLab/<stack>-ci/…@main`,
   secret passati per nome (mai `secrets: inherit`). Se lo stack non ha un reusable, si
   crea `<stack>-ci` con lo stesso schema, non si mette la logica nel progetto.
2. **Input comuni, uguali ovunque** (nome, valori, significato):
   - `runner`: `self-hosted` (default) | `github`;
   - `max_workers`: `auto` (default) | `2` | `4`;
   - `clear_cache`: boolean, semantica di `setup` (cartelle del progetto sì, cache
     condivise mai).
   Nel wrapper la descrizione di `max_workers` è sempre: «⚙️ Worker di Gradle/cargo.
   auto = le CPU libere di nexus-core in quel momento (mai sotto 2), su GitHub tutte.
   4 = tutta la macchina, 2 = lascia spazio a un'altra build.»
   Unica eccezione voluta: lo stack Expo (`expo-ci`) non ha `runner` ma `build_target`
   (`local` = self-hosted | `eas` = cloud di Expo), perché l'alternativa a nexus-core lì è
   EAS e non un runner GitHub. Per questo `ci-batch -f runner=…` non vale per `ascend` e
   `riftgate-mobile`.
3. **Nel reusable**, in quest'ordine: notifica di inizio (`build-kit/notify`), checkout con
   `clean: ${{ inputs.runner == 'github' }}` (o solo con `clear_cache`),
   `build-kit/setup`, eventuale `build-kit/x86-64`, pulizia delle sole cartelle del
   progetto se `clear_cache`, build, artefatto, notifica d'esito con `if: always()`.
   L'upload dell'artefatto ha `continue-on-error`: la quota di storage è dell'org (piano
   free, ricalcolata ogni 6-12 h) e piena non deve far fallire una build riuscita, che
   arriva comunque su Telegram. Resta bloccante solo dove un job successivo lo scarica
   (release di `desktop-ci`). Con `runner: github`, la cache di GitHub per ciò che sul
   self-hosted è cache della macchina (vedi sopra). Anche update e verifiche hanno
   notifica di inizio ed esito, e sul self-hosted non puliscono il checkout (la cartella di
   lavoro è quella delle build del repo) salvo `clear_cache` o un motivo scritto:
   `flutter-update` pulisce perché committa con `git add -A`.
4. **Niente installazioni di sistema dal workflow** su nexus-core: SDK condivisi
   (`/opt/android-sdk`, `~/sdk/flutter`) si usano, non si modificano; gli strumenti propri
   della CI stanno in `~/ci/tools` e l'azione che li usa sa installarli da sola.
5. **Parità fra progetti dello stesso stack** (oggi: Ascend e Riftgate su `expo-ci`,
   Kagami su `flutter-ci`, Riftgate desktop su `desktop-ci`): stessi input, stesse
   descrizioni, `run-name` con le scelte, esito nel topic Build (`telegram_topic_id: '6'`)
   per build **e** update, secret per nome. Una miglioria nasce nel reusable o qui, non in
   un wrapper; se tocca un wrapper, si porta negli altri nello stesso giro o si scrive
   perché no. Il controllo rapido è `ci-batch --list` più un `diff` fra i wrapper.
6. **Si misura prima e dopo**: tempi per step (`gh run view <id> --json jobs`) e, dentro
   la build, il log verboso con i tempi (per Flutter `-v`, per Gradle `--profile`).

## Pubblicare un progetto: repo privato + specchio pubblico

Lo sviluppo sta in un repo privato (`<nome>-private`), il pubblico (`<nome>`) riceve le
modifiche come PR preparate dal privato. Nato su Kagami, che resta il riferimento: il suo
`.github/workflows/CLAUDE.md` descrive lo stesso sistema dal lato del progetto.

```text
repo privato                                              specchio pubblico
  .github/workflows/public-sync.yml   thin wrapper: push su main, a mano (dry_run)
    └─ uses: build-kit/.github/workflows/public-sync.yml@main     runner self-hosted
         └─ uses: build-kit/public-sync@main   sync.py + modelli generici, stessa ref
  .github/public-sync/{exclude,patterns,allow}   del progetto, mai pubblici
        ── PR sync/<sha> di un commit, trailer Private-Sync ──→  main
                                                                  ├─ ci.yml       analisi e test, anche nei fork
                                                                  ├─ build.yml    ogni merge, runner di GitHub
                                                                  └─ release.yml  a mano: GitHub Release
```

### Come funziona il sync

- **Un commit per PR, mai la storia privata.** A ogni push su `main` del privato,
  `sync.py plan` costruisce l'albero di `main` meno i percorsi di `exclude` e ne fa un
  commit il cui unico genitore è `main` del pubblico, sul branch `sync/<sha corto>`. Il
  messaggio elenca i soggetti dei commit privati che toccano file pubblici. Un percorso
  escluso che esiste nel pubblico resta com'è. Albero uguale = niente da fare; una PR di
  sync nuova chiude quella ancora aperta.
- **Il trailer `Private-Sync: <sha>`** (ultima riga del commit e della descrizione della
  PR, che lo squash usa come messaggio) dice qual è l'ultimo commit privato arrivato: il
  sync seguente riparte da lì. Merge, squash o rebase vanno bene. Finché il pubblico non
  ha un trailer vale l'input `baseline`. Se il commit del trailer non è più un antenato
  (storia privata riscritta) il sync si ferma: si decide a mano.
- **Lo scanner blocca le fughe**, su ogni file dell'albero, sul messaggio e sulla
  descrizione. Il rapporto dice file, riga e id del modello, mai il valore. Un soggetto
  di commit che contiene un modello si omette invece di bloccare (resta per sempre nella
  storia privata). Eccezioni in `allow`, una per file e id, ciascuna motivata.
- **Un commit fatto direttamente nel pubblico non si perde.** Se la PR cancellerebbe
  cambiamenti del pubblico che non vengono da un sync, il sync si ferma, carica la patch
  come artefatto `public-sync` (30 giorni) e apre o commenta una issue nel privato. Si
  riporta con `git am`/`git apply`; se il riporto non è identico, il commit nel privato
  porta `Public-Sync-Accept: <sha di main pubblico>`.

### Scelte

- **Reusable più azione composita, non solo l'uno o l'altra.** Lo script deve girare alla
  stessa ref di chi lo chiama, e un reusable non conosce la propria (`github.workflow_ref`
  e `job.workflow_ref` sono del chiamante): un checkout di build-kit dal reusable dovrebbe
  cablare `main`. Un'azione invece ha `github.action_path`, quindi `public-sync/action.yml`
  fa una cosa sola: dà al job la cartella con `sync.py` e i modelli generici, scaricata da
  GitHub insieme all'azione. Tutto il resto sta nel reusable perché lì `failure()`,
  `job.status` e gli output di step valgono per il job intero, come negli altri reusable, e
  il runner (`runs_on`) è un input con default. Come per `notify`, il reusable usa
  `public-sync@main`.
- **Modelli dello scanner su tre livelli**, sommati: `public-sync/patterns` del kit (solo
  ciò che è segreto per chiunque: token, chiavi, keystore, configurazioni Firebase; il kit
  è pubblico e un modello dice già cosa cerca), `patterns` del progetto (ciò che riguarda
  solo lui) e il secret `PUBLIC_SYNC_PATTERNS` (quelli personali del manutentore, uguali in
  tutti i progetti: nomi di macchine, indirizzi, email, org dei servizi). Il secret finisce
  in un file del job con permessi 600, mai negli argomenti né nel log; un modello rotto
  ferma il sync senza stamparne il testo.
- **`PUBLIC_SYNC_PATTERNS` è un secret di repo, non d'organizzazione.** Sul piano Free i
  secret d'org non arrivano ai repo privati, cioè proprio a chi fa il sync. La fonte è un
  file solo, fuori da ogni repo, fra i segreti del manutentore; si copia in ogni privato
  con `gh secret set PUBLIC_SYNC_PATTERNS -R <owner>/<privato> < <file>`, e quando cambia
  si ripete per tutti. Con un piano a pagamento diventa un secret d'org visibile ai soli
  repo privati, con lo stesso nome, e wrapper e reusable restano uguali. Il wrapper passa
  `require_extra_patterns: true`: senza il secret il sync fallisce invece di pubblicare
  senza quei controlli.
- **Il sync gira solo nei repo privati** (`if: github.event.repository.private`), sul
  self-hosted: un minuto di git e python che non consuma minuti privati. `ubuntu-latest`
  non basterebbe a mandarlo su GitHub, perché uno dei self-hosted ne ha l'etichetta. Il job
  non installa niente e gira con `GIT_CONFIG_GLOBAL=/dev/null` e `GH_CONFIG_DIR` nel temp:
  la config globale della macchina passerebbe a git le credenziali di `gh`, che non devono
  entrare nel push. Il pubblico si tocca solo con `PUBLIC_SYNC_TOKEN`, mai col
  `GITHUB_TOKEN`, che serve solo per la issue nel privato.
- **Nel pubblico build e release girano sui runner di GitHub.** Il runner group «Default»
  dell'org non serve i repo pubblici, quindi il self-hosted lì non c'è (un wrapper
  self-hosted portato dal sync resta in coda se lanciato), e un repo pubblico ha minuti
  gratis, macOS compreso. Per lo stesso motivo lì si usano solo azioni e reusable
  pubblici: `flutter-ci`, `desktop-ci`, `expo-ci` e questo repo. Nel privato gli stessi
  workflow partono solo a mano (un minuto macOS del piano ne vale dieci).
- **I template stanno qui e non nei `<stack>-ci`**: la pubblicazione attraversa gli stack
  (il sync è uguale per tutti) e la checklist è una sola. Si copiano, non si referenziano:
  un template cambiato non tocca i progetti che l'hanno già copiato, e la parità si porta
  a mano come per i wrapper.

### Secret

| Secret | Dove | Cosa |
| :--- | :--- | :--- |
| `PUBLIC_SYNC_TOKEN` | privato | PAT fine-grained, owner l'org, «Only select repositories» = il solo pubblico; Contents, Pull requests e Workflows in lettura e scrittura (senza Workflows GitHub rifiuta ogni sync che tocca `.github/workflows/`). Scade: il sync fallito arriva su Telegram, si rigenera e si rimette con `gh secret set PUBLIC_SYNC_TOKEN -R <owner>/<privato>`. |
| `PUBLIC_SYNC_PATTERNS` | privato | i modelli personali, vedi sopra. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | privato e, se si vogliono gli esiti, pubblico | senza, la notifica non parte e il run non avvisa (è il caso di ogni fork). |
| secret di build (`ANDROID_KEYSTORE*`, `GOOGLE_SERVICES_JSON`, `SENTRY_DSN`, `TAURI_SIGNING_*`, `EXPO_TOKEN`) | pubblico | quelli dei template dello stack; la chiave di release è sempre la stessa, o chi ha l'app non la aggiorna più. |

### Checklist per un progetto nuovo

1. **Privato pronto**: `CLAUDE.md`, niente segreti nei file tracciati. Copiare
   `templates/publish/public-sync/{exclude,patterns,allow}` in `.github/public-sync/` e
   riempire `exclude` (il sync stesso, note locali, configurazioni di chi compila, workflow
   solo del manutentore) e `patterns` (i modelli propri del progetto).
2. **Prova dello scanner** in locale, sull'albero di `main`:
   `python3 <build-kit>/public-sync/sync.py scan HEAD --extra-patterns <file dei modelli personali>`.
   Ogni occorrenza si toglie, si esclude, o diventa un'eccezione motivata in `allow`.
3. **Crea il pubblico e il commit iniziale** con l'albero filtrato, senza storia: in un
   clone del privato, `git read-tree HEAD`, `git rm --cached -r -q` dei percorsi esclusi,
   `git commit-tree $(git write-tree) -m "<Nome>"` e push di quel commit come `main` del
   pubblico nuovo (vuoto). Lo SHA del commit **privato** di partenza è la `baseline`.
4. **Secret**: `PUBLIC_SYNC_TOKEN` e `PUBLIC_SYNC_PATTERNS` nel privato (vedi sopra),
   quelli di build nel pubblico.
5. **Wrapper**: `templates/publish/public-sync.yml` in `.github/workflows/` del privato,
   con `app_name`, `public_repo` e `baseline`.
6. **Build e release dello specchio**: i template dello stack in `.github/workflows/`
   del privato (arrivano al pubblico col sync), placeholder `<…>` sostituiti.
7. **Prova a secco**: `gh workflow run public-sync.yml -R <owner>/<privato> -f dry_run=true`,
   poi il riepilogo del run: file che cambiano, commit, scanner.
8. **Prima PR**: un push su `main` (o il run senza `dry_run`); si controlla la PR nel
   pubblico, si unisce, e da lì il trailer sostituisce la baseline. `build.yml` parte sul
   merge: si verificano gli artefatti (firma con `apksigner verify --print-certs`, per
   esempio).
9. **Release**: si alza la versione nel privato, il sync la porta, e dal pubblico si
   lancia `release.yml` a mano su `main`.

Da ricordare: i commit fatti col `GITHUB_TOKEN` (per esempio un aggiornamento delle
dipendenze che committa da solo) non fanno partire workflow, quindi nemmeno il sync: va
lanciato a mano o aspetta il push seguente.

### Cosa c'è per ogni stack

- **Flutter** (`templates/publish/flutter/`): completo. `ci.yml` autonomo (analisi,
  test, APK a mano; niente reusable, così funziona in ogni fork), `build.yml` su
  `flutter-ci` con `runner: github` (APK per architettura, universale, IPA non firmato),
  `release.yml` (versione da `pubspec.yaml`, controlli di `ci.yml`, build di `build.yml`,
  rifiuto di un APK firmato con una chiave di debug, SHA-256 di tutti i file).
- **Tauri desktop** (`templates/publish/desktop/`): `build.yml` e `release.yml` su
  `desktop-ci` con `runner_type: github`; la release va nel pubblico stesso con il token del
  run (`RELEASES_TOKEN: ${{ github.token }}` e `contents: write`). Manca, in `desktop-ci`:
  il titolo e le note della release sono ancora cablati su WarpMobile, nella release
  vanno solo i file con una firma dell'updater (niente `.deb`, `.rpm`, `.dmg`), niente
  SHA-256, e un `ci.yml` di analisi e test non c'è (è del progetto).
- **Expo** (`templates/publish/expo/`): solo `build.yml` su EAS, a mano. Nel pubblico
  `expo-ci` ha solo EAS: la build locale è cablata sul self-hosted, e con EAS l'APK
  resta sui server di Expo (nel run e su Telegram arriva l'esito, non il file). Per una
  release come quella di Flutter mancano, in `expo-ci`: una build locale su
  `ubuntu-latest` (con la firma dai secret `ANDROID_KEYSTORE_*`), oppure lo scaricamento
  dell'artefatto EAS (`eas build --json` dà l'URL) come artefatto del run; poi un
  `release.yml` che lo pubblichi.

## Convenzioni

Nomi di input in inglese, descrizioni e commenti in italiano; i commenti spiegano il
perché. Commit atomici, messaggi in italiano.
