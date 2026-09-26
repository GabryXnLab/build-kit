# build-kit

Componenti comuni delle build di GabryXnLab, usati dai reusable
[`flutter-ci`](https://github.com/GabryXnLab/flutter-ci),
[`expo-ci`](https://github.com/GabryXnLab/expo-ci) e
[`desktop-ci`](https://github.com/GabryXnLab/desktop-ci). Il perché e il modello da seguire
per una build nuova stanno in [`CLAUDE.md`](CLAUDE.md).

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

Repo privato: `Settings → Actions → Access` consente l'uso ai repo dell'organizzazione,
come `ci-bot`.
