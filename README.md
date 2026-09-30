# build-kit

Componenti comuni delle build di GabryXnLab, usati dai reusable
[`flutter-ci`](https://github.com/GabryXnLab/flutter-ci),
[`expo-ci`](https://github.com/GabryXnLab/expo-ci) e
[`desktop-ci`](https://github.com/GabryXnLab/desktop-ci), e il sync che pubblica un repo
privato nel suo specchio pubblico. Il perché e il modello da seguire per una build nuova
stanno in [`CLAUDE.md`](CLAUDE.md).

## `setup` — worker e cache

```yaml
- uses: GabryXnLab/build-kit/setup@main
  with:
    runner: ${{ inputs.runner }}            # self-hosted | github
    max_workers: ${{ inputs.max_workers }}  # auto | 2 | 4
    clear_cache: ${{ inputs.clear_cache }}
    rust: 'true'                            # solo per chi compila Rust
```

Scrive in `GITHUB_ENV`:

| Variabile | Valore | Perché |
| :--- | :--- | :--- |
| `GRADLE_OPTS` | `-Dorg.gradle.workers.max=N -Dorg.gradle.parallel=true` (+ `-Dorg.gradle.caching=false` con `clear_cache`) | prevale su `~/.gradle/gradle.properties` della macchina, solo per il run |
| `CARGO_BUILD_JOBS`, `CMAKE_BUILD_PARALLEL_LEVEL` | `N` | stessi worker per cargo e CMake |
| `NDK_CCACHE`, `CCACHE_BASEDIR=$HOME`, `CCACHE_NOHASHDIR` | self-hosted | il C/C++ dell'NDK passa da ccache, riusabile fra runner e progetti |
| `RUSTC_WRAPPER=sccache`, `SCCACHE_DIR`, `CARGO_TARGET_DIR`, `CARGO_INCREMENTAL=0` | self-hosted con `rust` | dipendenze Rust compilate una volta per tutti; target fuori dal checkout |
| `CCACHE_RECACHE`, `SCCACHE_RECACHE` | con `clear_cache` | ricompila senza fidarsi della cache, e la riscrive |

`N` con `auto`: sul self-hosted `nproc` meno il carico del momento, minimo 2; su GitHub
`nproc`. Output: `workers`, `cargo_target_dir`.

## `x86-64` — binari x86-64 sul runner ARM64

```yaml
- id: x86
  uses: GabryXnLab/build-kit/x86-64@main
  with:
    emulator: box64        # box64 | qemu
- run: ${{ steps.x86.outputs.launcher }} ./tool-x86-64 --args
```

Su un host x86-64 `launcher` è vuoto. Lanciatori in `~/ci/tools/bin/x86-64-{box64,qemu}`,
Box64 in `~/ci/tools/box64` (compilato da sé se manca), QEMU 10 in `~/qemu-x86_64-10`.

## `notify` — esito su Telegram

```yaml
- id: started
  uses: GabryXnLab/build-kit/notify@main
  with:
    bot_token: ${{ secrets.TELEGRAM_BOT_TOKEN }}
    chat_id: ${{ secrets.TELEGRAM_CHAT_ID }}
    topic_id: ${{ inputs.telegram_topic_id }}
    status: started                          # all inizio: «in corso» con Annulla
    title: ${{ inputs.app_name }} android release
# … la build …
- if: ${{ always() }}
  uses: GabryXnLab/build-kit/notify@main
  with:
    message_id: ${{ steps.started.outputs.message_id }}   # riscrive lo stesso messaggio
    bot_token: ${{ secrets.TELEGRAM_BOT_TOKEN }}
    chat_id: ${{ secrets.TELEGRAM_CHAT_ID }}
    topic_id: ${{ inputs.telegram_topic_id }}
    status: ${{ job.status }}
    title: ${{ inputs.app_name }} android release
    files: build/app/outputs/flutter-apk/*.apk            # un modello per riga
```

Best-effort, non fa mai fallire il job. Manda solo in un topic di un supergruppo
(`chat_id` `-100…`, mai in privato né nel General). **Senza token o chat non fa niente e
non avvisa**: è il caso di un repo pubblico senza i secret di Telegram e di ogni fork.
Gli artefatti partono come documenti fino a 50 MB, fino a ~2 GB se sul runner risponde un
Local Bot API Server (`TELEGRAM_API_BASE`, default `http://localhost:8081`). I pulsanti
(Annulla, Rilancia, Errore, Risolvi con AI, Rimanda artefatti) sono callback per il bot
che li riceve: con `ci-bot` funzionano, con un altro bot restano muti.

È la copia pubblica di `ci-bot/notify`, con gli stessi input: `ci-bot` resta privato, e un
repo pubblico non può usare un'azione di un repo privato.

## `public-sync` — specchio pubblico di un repo privato

Porta `main` del repo privato nel suo specchio pubblico come PR di un commit solo, meno i
file esclusi, dopo uno scanner anti-fuga. Architettura, secret e checklist per un progetto
nuovo: [`CLAUDE.md`](CLAUDE.md), «Pubblicare un progetto». Il wrapper da copiare nel privato
è [`templates/publish/public-sync.yml`](templates/publish/public-sync.yml):

```yaml
jobs:
  sync:
    uses: GabryXnLab/build-kit/.github/workflows/public-sync.yml@main
    with:
      app_name: App
      public_repo: <owner>/<pubblico>
      baseline: <sha del commit privato da cui è nato il pubblico>
      require_extra_patterns: true
      dry_run: ${{ inputs.dry_run == true }}
    secrets:
      PUBLIC_SYNC_TOKEN: ${{ secrets.PUBLIC_SYNC_TOKEN }}        # PAT sul solo pubblico
      PUBLIC_SYNC_PATTERNS: ${{ secrets.PUBLIC_SYNC_PATTERNS }}  # modelli personali
      TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
      TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
```

| Input | Default | |
| :--- | :--- | :--- |
| `config_dir` | `.github/public-sync` | `exclude` (obbligatorio), `patterns`, `allow` del progetto |
| `branch_prefix` | `sync/` | branch della PR nel pubblico |
| `sync_trailer`, `accept_trailer` | `Private-Sync`, `Public-Sync-Accept` | |
| `author_name`, `author_email` | `github-actions[bot]` | autore del commit di sync |
| `runs_on` | `["self-hosted", "nexus-core"]` | JSON |
| `telegram_topic_id` | `'6'` | vuoto = nessuna notifica |

Output `result`: `noop`, `ready`, `pr-up-to-date`, `blocked-public` (contributi del
pubblico: patch nell'artefatto `public-sync` e una issue nel privato), `blocked-leak`.

Lo script gira anche in locale, in un clone del privato con `main` del pubblico in
`refs/public/main` (crea solo oggetti e un ref `refs/public-sync-out/…`):

```bash
git fetch https://github.com/<owner>/<pubblico>.git '+refs/heads/main:refs/public/main'
python3 public-sync/sync.py plan --dry-run --baseline <sha> --extra-patterns <file> --out-dir /tmp/x
python3 public-sync/sync.py scan HEAD --extra-patterns <file>
python3 -B -m unittest public-sync/test_sync.py      # le prove, su repo git finti
```

`templates/publish/` ha anche build e release dello specchio: `flutter/` (`ci.yml`,
`build.yml`, `release.yml`), `desktop/` (`build.yml`, `release.yml`), `expo/` (`build.yml`
su EAS).

## `bin/ci-batch` — più build in un colpo

```bash
~/repos/build-kit/bin/ci-batch --list
~/repos/build-kit/bin/ci-batch kagami riftgate-mobile riftgate-desktop
~/repos/build-kit/bin/ci-batch -f max_workers=2 kagami riftgate-mobile@feature-x
```

Le build vanno sui due runner di nexus-core man mano che si liberano; gli esiti arrivano
nel topic Build di Telegram. Un progetto nuovo si aggiunge alla tabella `TARGETS`.

## Cosa c'è sulla macchina

| Cosa | Dove | Condiviso fra |
| :--- | :--- | :--- |
| runner | `~/ci/actions-runner` (`nexus-core`), `~/ci/actions-runner-2` (`nexus-core-2`) | — |
| Gradle: dipendenze, trasformazioni, build cache | `~/.gradle` | tutti i progetti Gradle, entrambi i runner |
| pub | `~/.pub-cache` | progetti Flutter |
| pnpm | store di pnpm (`~/.local/share/pnpm/store`) | progetti Node |
| ccache | `~/.cache/ccache` (10 GB) | NDK di tutti i progetti |
| sccache | `~/.cache/sccache` (10 GB) | Rust di tutti i progetti |
| target di cargo | `~/ci/cache/cargo-target/<repo>` | workflow dello stesso repo |
| Box64, sccache, lanciatori | `~/ci/tools` | — |

## Visibilità

Repo pubblico: lo usano anche i reusable chiamati da repo pubblici (`flutter-ci`), e GitHub
risolve ogni `uses:` all'avvio del job, anche quello di uno step che non girerà. Qui non
c'è niente di segreto: token, chat e chiavi arrivano sempre dal chiamante.
