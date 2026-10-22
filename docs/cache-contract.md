# Contratto della cache remota (bucket S3)

Interfaccia fra chi scrive la cache (`setup`, init script di Gradle, la futura azione `s3-cache`) e chi la
pulisce (il futuro gestore Rust). Un cambio incompatibile alza la radice (`v2/`), non riscrive `v1/`.

## Bucket e credenziali

- Bucket S3-compatibile, path-style, una sola chiave in lettura e scrittura. Nome, endpoint e regione non
  stanno in questo repo (pubblico): arrivano con le credenziali, in un file `.env` con le chiavi
  `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `S3_ENDPOINT` (con `https://`), `S3_BUCKET` e,
  facoltativa, `GRADLE_REMOTE_CACHE_URL`.
- Da dove: input `cache_env` di `setup` (runner effimeri: un secret di repo passato per nome), altrimenti il
  file della macchina, `~/secrets/oci-build-cache.env` (nexus-core) o `/var/lib/ci/secrets/oci-build-cache.env`
  (ohos). Senza credenziali la cache remota si salta, la build no. Repo pubblici e fork mai.

## Prefissi (tutto sotto `v1/`)

| Prefisso | Chi scrive | Contenuto |
| :--- | :--- | :--- |
| `v1/rust/sccache/<arch>/` | sccache (`SCCACHE_S3_KEY_PREFIX`), `arm64` o `x64`, comune a tutti i runner; anche il C/C++ di CMake (`setup` con `cmake_cache`) | oggetti di sccache, formato suo |
| `v1/gradle/build-cache/<chiave>` | Gradle `HttpBuildCache` | voci della build cache, chiave = hash di Gradle |
| `v1/deps/<stack>/<tipo>/<os-arch>/<chiave>.tar.zst` | `s3-cache` (fase 4b) | archivi di dipendenze |
| `v1/index/deps/<stack>/<tipo>/<os-arch>/<chiave>.tar.zst.json` | `s3-cache` | `last_used` dell'archivio (percorso dell'archivio con `v1/index/` al posto di `v1/` e `.json` in coda) |

- `<profilo>`: `nexus` | `ohos` | `ubicloud` | `github` (il valore di `runner` di `setup`, `self-hosted` =
  `nexus`); `<arch>`: `x64` | `arm64`. Profili diversi non si mescolano: compilatori e percorsi cambiano.
- `<tipo>`: `pnpm-store` | `pub-cache` | `gradle-modules` | `gradle-wrapper` | `cargo-registry`.
  `<chiave>` = hash del lockfile più la versione dello strumento: chiave diversa, archivio nuovo.
- Gradle scrive tramite un URL pre-autenticato (PAR) limitato al suo prefisso: chi ha l'URL legge e scrive
  solo lì, non elenca il bucket.

## Metadati e indice

- Gli archivi di `deps/` portano `x-amz-meta-gx-schema` (`1`), `gx-repo`, `gx-stack`, `gx-tool-version`,
  `gx-arch`, `gx-run` (id del run che l'ha scritto). sccache e Gradle scrivono senza metadati nostri.
- Una GET non cambia l'oggetto, quindi l'uso si registra a parte: a ogni hit `s3-cache` riscrive
  `v1/index/deps/<stack>/<tipo>/<os-arch>/<chiave>.tar.zst.json` con `{"last_used": "<ISO 8601 UTC>", "run": "<id>"}`. Per sccache e Gradle vale
  la data di scrittura dell'oggetto.

## Quote e pulizia

- Budget 50 GB: indicativi sccache 15, gradle 15, deps 15, margine 5 GB.
- Finché il gestore non c'è: regola di lifecycle del bucket, 30 giorni dalla scrittura per prefisso.
- Il gestore cancella **solo sotto `v1/`**, in quest'ordine: archivi `deps/` incompatibili (`gx-tool-version`
  diversa da quelle fissate nei reusable) e il loro indice; poi, per prefisso oltre quota, i meno usati
  (`last_used`, altrimenti data di scrittura). Nessuna voce è indispensabile: una cancellata è un miss.
