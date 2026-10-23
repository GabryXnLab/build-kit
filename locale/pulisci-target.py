#!/usr/bin/env python3
"""Tiene a bada le cartelle target di cargo delle build a mano e degli agenti (timer di utente, installa.sh).

  pulisci-target.py [--dry-run] [--radice DIR …] [--soglia-gb 25] [--critica-gb 10] [--giorni 5]

Sempre:            incrementali e eseguibili di test fermi da 2 ore; target intere non usate da --giorni.
Sotto --soglia-gb: incrementali e test fermi da 15 minuti; profili (debug/, release/, <triple>/<profilo>/)
                   non usati da 6 ore, dalla target più grande.
Sotto --critica-gb: profili non usati da 30 minuti, dalla target più grande, finché si torna sopra la soglia.

Perché si può essere aggressivi: con gx-sccache (catena disco + S3) ricompilare le dipendenze è un hit da
~30 ms a crate; si ricompilano davvero solo i crate del workspace. Una target non si tocca se è in uso:
lock di cargo (.cargo-lock, .cargo-build-lock, .cargo-artifact-lock) e lock di progetto in cima alla target
(es. .athena-cargo.lock) presi in esclusiva e tenuti per tutta la cancellazione, nessun processo con
eseguibile o cwd dentro la target, nessun cargo vivo nello stesso repo (cargo test esegue i binari dopo
aver lasciato il lock) o con CARGO_TARGET_DIR uguale. Le target della CI (~/ci) non sono sotto le radici.
"""
import argparse, fcntl, os, re, shutil, sys, time

HOME = os.path.expanduser("~")
ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--radice", action="append", help="default: ~/repos e /tmp")
ap.add_argument("--soglia-gb", type=float, default=25)
ap.add_argument("--critica-gb", type=float, default=10)
ap.add_argument("--giorni", type=float, default=5)
ap.add_argument("--attesa", type=float, default=0,
                help="secondi: sotto la soglia critica riprova le target occupate fino a questo tempo")
a = ap.parse_args()
roots = [os.path.realpath(r) for r in (a.radice or [os.path.join(HOME, "repos"), "/tmp"])]
dev_root = os.stat("/").st_dev
now = time.time()
LOCKS = {".cargo-lock", ".cargo-build-lock", ".cargo-artifact-lock"}
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".gradle", "build", "dist", ".pnpm-store"}
TEST_EXE = re.compile(r"^[A-Za-z0-9_]+-[0-9a-f]{16}$")


def free_gb():
    st = os.statvfs("/")
    return st.f_bavail * st.f_frsize / 2**30


def is_cargo_target(d):
    # CACHEDIR.TAG manca nelle target create da altri prima di cargo (athena: mkdir del lock).
    if os.path.isfile(os.path.join(d, ".rustc_info.json")):
        return True
    try:
        with open(os.path.join(d, "CACHEDIR.TAG")) as f:
            return "cargo" in f.read(300)
    except OSError:
        return False


def find_targets():
    out = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for d, subs, _ in os.walk(root):
            depth = d[len(root):].count(os.sep)
            try:
                if os.stat(d).st_dev != dev_root:
                    subs[:] = []
                    continue
            except OSError:
                subs[:] = []
                continue
            if is_cargo_target(d):
                out.add(os.path.realpath(d))
                subs[:] = []
                continue
            subs[:] = [s for s in subs if s not in SKIP and depth < 7 and not os.path.islink(os.path.join(d, s))]
    return sorted(out)


def repo_of(path):
    """Primo livello sotto la radice che lo contiene: il repo, anche per le worktree in .claude/worktrees."""
    for root in roots:
        if path.startswith(root + os.sep):
            return os.path.join(root, path[len(root) + 1:].split(os.sep)[0])
    return path


def profile_dirs(t):
    """Cartelle con deps/ e .fingerprint/: target/<profilo> e target/<triple>/<profilo>."""
    res = []
    for e in os.scandir(t):
        if not e.is_dir(follow_symlinks=False):
            continue
        if os.path.isdir(os.path.join(e.path, ".fingerprint")):
            res.append(e.path)
        else:
            for f in os.scandir(e.path):
                if f.is_dir(follow_symlinks=False) and os.path.isdir(os.path.join(f.path, ".fingerprint")):
                    res.append(f.path)
    return res


def last_use(p):
    m = 0
    for sub in ("", "deps", ".fingerprint", "build"):
        try:
            m = max(m, os.stat(os.path.join(p, sub)).st_mtime)
        except OSError:
            pass
    for n in LOCKS:
        try:
            m = max(m, os.stat(os.path.join(p, n)).st_mtime)
        except OSError:
            pass
    return m


def size(p):
    t = 0
    for d, _, fs in os.walk(p):
        for f in fs:
            try:
                t += os.lstat(os.path.join(d, f)).st_blocks * 512
            except OSError:
                pass
    return t


def processes():
    """(exe, cwd, è cargo, CARGO_TARGET_DIR) dei processi leggibili."""
    res = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        cargo = os.path.basename(exe) == "cargo"
        ctd = None
        if cargo:
            try:
                with open(f"/proc/{pid}/environ", "rb") as f:
                    for kv in f.read().split(b"\0"):
                        if kv.startswith(b"CARGO_TARGET_DIR="):
                            ctd = os.path.realpath(os.path.join(cwd, kv[17:].decode(errors="replace")))
            except OSError:
                pass
        res.append((exe, os.path.realpath(cwd), cargo, ctd))
    return res


def busy(t, procs):
    pre = t + os.sep
    rep = repo_of(t) + os.sep
    # Un lock di progetto (athena: ogni cargo degli agenti, test compresi, gira sotto flock) basta da solo:
    # con quattro agenti un cargo vivo nel repo c'è quasi sempre e la target non si pulirebbe mai.
    project_lock = any(n.startswith(".") and n.endswith(".lock") for n in os.listdir(t))
    for exe, cwd, cargo, ctd in procs:
        if exe.startswith(pre) or (cwd + os.sep).startswith(pre):
            return f"processo in esecuzione dentro ({exe})"
        if cargo and not project_lock and ((cwd + os.sep).startswith(rep) or ctd == t):
            return "cargo vivo nello stesso repo"
    return None


def take_locks(t):
    """Lock esclusivi non bloccanti; None se uno è occupato (build in corso)."""
    files = [os.path.join(t, n) for n in os.listdir(t) if n.startswith(".") and n.endswith(".lock")]
    for p in profile_dirs(t):
        files += [os.path.join(p, n) for n in LOCKS if os.path.exists(os.path.join(p, n))]
    held = []
    for f in files:
        try:
            fd = open(f, "a")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(fd)
        except BlockingIOError:
            for h in held:
                h.close()
            return None
        except OSError:
            continue
    return held


freed = {}
removed = set()


def eff_free():
    """Disco libero, contando nel dry-run ciò che sarebbe già stato tolto."""
    return free_gb() + (sum(freed.values()) / 2**30 if a.dry_run else 0)


def rm(path, kind):
    if any(path == r or path.startswith(r + os.sep) for r in removed):
        return
    removed.add(path)
    sz = size(path) if os.path.isdir(path) else os.lstat(path).st_blocks * 512
    freed[kind] = freed.get(kind, 0) + sz
    if sz > 2**28:
        print(f"  {'toglierebbe' if a.dry_run else 'tolta'}: {path} ({sz / 2**30:.2f} GB, {kind})")
    if a.dry_run:
        return
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def light(t, minutes):
    """Incrementali e eseguibili di test fermi da `minutes`: si rigenerano alla prossima build."""
    lim = now - minutes * 60
    for p in profile_dirs(t):
        inc = os.path.join(p, "incremental")
        if os.path.isdir(inc):
            for e in os.scandir(inc):
                if e.stat(follow_symlinks=False).st_mtime < lim:
                    rm(e.path, "incrementali")
        deps = os.path.join(p, "deps")
        if os.path.isdir(deps):
            for e in os.scandir(deps):
                if not TEST_EXE.match(e.name) or not e.is_file(follow_symlinks=False):
                    continue
                st = e.stat(follow_symlinks=False)
                # nlink > 1: hardlink del binario dell'app in <profilo>/, che resta.
                if st.st_mode & 0o111 and st.st_nlink == 1 and max(st.st_mtime, st.st_atime) < lim:
                    rm(e.path, "eseguibili di test")


def main():
    targets = find_targets()
    start = free_gb()
    deadline = time.time() + a.attesa
    while True:
        skipped = one_pass(targets)
        if not skipped or eff_free() >= a.critica_gb or time.time() >= deadline:
            break
        time.sleep(15)
    tot = sum(freed.values()) / 2**30
    verb = "libererebbe" if a.dry_run else "liberati"
    print(f"pulisci-target{' (dry-run)' if a.dry_run else ''}: {len(targets)} target, {verb} {tot:.2f} GB"
          + "".join(f"; {k} {v / 2**30:.2f} GB" for k, v in freed.items())
          + f"; disco libero {start:.1f} → {free_gb():.1f} GB")
    for s in skipped:
        print("  saltata:", s)


def one_pass(targets):
    global now
    now = time.time()
    procs = processes()
    skipped = []

    def guarded(t, fn):
        why = busy(t, procs)
        if why:
            skipped.append(f"{t}: {why}")
            return False
        held = take_locks(t)
        if held is None:
            skipped.append(f"{t}: lock occupato")
            return False
        try:
            fn()
        finally:
            for h in held:
                h.close()
        return True

    for t in targets:
        if not os.path.isdir(t):
            continue
        profs = profile_dirs(t)
        if profs and now - max(last_use(p) for p in profs) > a.giorni * 86400:
            guarded(t, lambda t=t: rm(t, f"target ferme da {a.giorni:g} giorni"))
            continue
        guarded(t, lambda t=t: light(t, 15 if eff_free() < a.soglia_gb else 120))

    for limit_h, gate in ((6, a.soglia_gb), (0.5, a.critica_gb)):
        if eff_free() >= gate:
            continue
        cands = []
        for t in targets:
            if os.path.isdir(t):
                for p in profile_dirs(t):
                    if now - last_use(p) > limit_h * 3600:
                        cands.append((size(p), t, p))
        for sz, t, p in sorted(cands, reverse=True):
            if eff_free() >= a.soglia_gb:
                break
            guarded(t, lambda p=p: rm(p, f"profili fermi da {limit_h:g} h"))
    return skipped


main()
