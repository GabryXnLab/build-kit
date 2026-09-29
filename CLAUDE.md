# CLAUDE.md — build-kit

## Cos'è

I **componenti comuni delle build** di tutti i progetti dell'organizzazione: ciò che
`flutter-ci`, `expo-ci` e `desktop-ci` facevano ognuno a modo suo (quanti worker, quali
cache, cosa vuol dire «pulire») sta qui una volta sola, come azioni composite, più lo
script che lancia più build insieme. È il posto da cui parte chi scrive o rivede un
workflow di build: la sezione «Come si scrive una build» è il modello da seguire.

`README.md` ha l'uso; questo file i vincoli e il perché.

## Stack

Azioni composite di GitHub Actions (bash con `set -euo pipefail`) e uno script bash.
Nessuna dipendenza, niente da compilare.

## Struttura

```text
setup/action.yml     worker (max_workers), cache condivise della macchina, clear_cache di sola esecuzione
x86-64/action.yml    lanciatore per binari x86-64 sul runner ARM64: Box64 (default) o QEMU 10
bin/ci-batch         lancia le build di più progetti in un colpo (gh workflow run)
```

## Comandi

```bash
python3 -c "import yaml,glob; [yaml.safe_load(open(f)) for f in glob.glob('*/action.yml')]"
bash -n bin/ci-batch && bin/ci-batch --list
```

## Architettura delle build

```text
progetto (thin wrapper: solo scelte)          es. kagami/.github/workflows/build-android.yml
  └─ uses: GabryXnLab/<stack>-ci/…@main        logica dello stack: flutter-ci | expo-ci | desktop-ci
       ├─ uses: GabryXnLab/build-kit/setup@main   worker, cache condivise, clear_cache
       ├─ uses: GabryXnLab/build-kit/x86-64@main  solo se serve un binario x86-64
       └─ uses: GabryXnLab/ci-bot/notify@main     esito su Telegram
```

Runner: `[self-hosted, nexus-core]` — **due istanze** sulla stessa macchina
(`~/ci/actions-runner` e `~/ci/actions-runner-2`, stesso utente), quindi due build in
parallelo e cache della macchina in comune. `runner: github` resta l'eccezione.

## Note architetturali importanti

- **`@main` è live per tutti**: i tre reusable usano `build-kit/…@main`. Input nuovi con
  default, mai rinominati né tolti senza aggiornare i chiamanti.
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
3. **Nel reusable**, in quest'ordine: notifica di inizio (`ci-bot/notify`), checkout con
   `clean: ${{ inputs.runner == 'github' }}` (o solo con `clear_cache`),
   `build-kit/setup`, eventuale `build-kit/x86-64`, pulizia delle sole cartelle del
   progetto se `clear_cache`, build, artefatto, notifica d'esito con `if: always()`.
   L'upload dell'artefatto ha `continue-on-error`: la quota di storage è dell'org (piano
   free, ricalcolata ogni 6-12 h) e piena non deve far fallire una build riuscita, che
   arriva comunque su Telegram. Resta bloccante solo dove un job successivo lo scarica
   (release di `desktop-ci`).
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

## Convenzioni

Nomi di input in inglese, descrizioni e commenti in italiano; i commenti spiegano il
perché. Commit atomici, messaggi in italiano.
