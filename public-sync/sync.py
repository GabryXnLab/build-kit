#!/usr/bin/env python3
"""Prepara il commit che porta main di un repo privato nel suo specchio pubblico.

Solo calcolo locale, niente rete: il workflow fa prima i fetch e dopo push e PR.
Nel repo di lavoro servono il commit privato (HEAD) con la sua storia e main del
pubblico in `--public-ref`; i branch di sync già aperti, se ci sono, sotto
`--pr-prefix`.

  sync.py plan [--head REF] [--public-ref REF] [--out-dir DIR] [--baseline SHA] ...
  sync.py scan TREEISH        lo scanner da solo, per esempio sull'albero pubblico

La configurazione del progetto sta in `--config-dir` (default `.github/public-sync`):
`exclude` (obbligatorio), `patterns` e `allow`. Ai modelli del progetto si sommano
sempre quelli generici del kit (`patterns` accanto a questo script) e quelli dei file
`--extra-patterns`, pensati per i modelli personali che non stanno in nessun repo.

Il commit nuovo ha come unico genitore main del pubblico: la storia privata non
entra nel push, ci entrano solo l'albero filtrato e il messaggio.
"""

import argparse
import fnmatch
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent
MESSAGE_LABEL = "(messaggio di commit)"
PR_BODY_LABEL = "(descrizione della PR)"

# Impostati da main() con gli argomenti: le funzioni sotto li leggono da qui.
CONFIG_DIR = Path(".github/public-sync")
EXTRA_PATTERNS = []
SYNC_TRAILER = "Private-Sync"
# Nel privato, dopo aver riportato a mano dei contributi del pubblico: dice che i
# cambiamenti pubblici fino a quel commit si possono sovrascrivere.
ACCEPT_TRAILER = "Public-Sync-Accept"


def git(*args, input=None, env=None, check=True):
    result = subprocess.run(
        ["git", *args],
        input=input,
        capture_output=True,
        env={**os.environ, **(env or {})},
    )
    if check and result.returncode != 0:
        sys.exit(f"git {' '.join(args)}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout.decode(errors="replace")


def read_rules(path, required=False):
    if not path.is_file():
        if required:
            sys.exit(f"Manca {path}.")
        return []
    rules = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            rules.append(line)
    return rules


def load_excludes():
    return read_rules(CONFIG_DIR / "exclude", required=True)


def is_excluded(path, excludes):
    for pattern in excludes:
        if pattern.endswith("/"):
            prefix = pattern.lstrip("/")
            if path.startswith(prefix):
                return True
        elif "/" in pattern:
            if fnmatch.fnmatchcase(path, pattern.lstrip("/")):
                return True
        elif fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern):
            return True
    return False


def ls_tree(treeish):
    """percorso -> (mode, type, sha) di tutti i file dell'albero."""
    entries = {}
    for record in git("ls-tree", "-r", "-z", "--full-tree", treeish).split("\0"):
        if record:
            meta, path = record.split("\t", 1)
            mode, kind, sha = meta.split()
            entries[path] = (mode, kind, sha)
    return entries


def public_part(entries, excludes):
    return {p: e for p, e in entries.items() if not is_excluded(p, excludes)}


def write_tree(entries):
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        info = "".join(f"{m} {s}\t{p}\0" for p, (m, _, s) in sorted(entries.items()))
        git("update-index", "-z", "--index-info", input=info.encode(), env=env)
        return git("write-tree", env=env).strip()


def trailer_values(ref, key, rev_range=None):
    """(commit, valore) dei commit che hanno il trailer, dal più recente."""
    out = git(
        "log",
        f"--format=%H%x1f%(trailers:key={key},valueonly,separator=%x1e)%x1d",
        rev_range or ref,
    )
    found = []
    for record in out.split("\x1d"):
        record = record.strip()
        if not record:
            continue
        commit, _, values = record.partition("\x1f")
        values = [v.strip() for v in values.split("\x1e") if v.strip()]
        if values:
            found.append((commit, values[-1]))
    return found


def commit_exists(sha):
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True).returncode == 0


def is_ancestor(a, b):
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], capture_output=True).returncode == 0


# --- scanner -----------------------------------------------------------------


def pattern_files():
    """(etichetta, file) dei modelli, in ordine: kit, progetto, extra."""
    files = [("kit", KIT_DIR / "patterns"), ("progetto", CONFIG_DIR / "patterns")]
    files += [("extra", Path(p)) for p in EXTRA_PATTERNS]
    return files


def load_patterns():
    patterns = []
    for label, path in pattern_files():
        for number, line in enumerate(read_rules(path), 1):
            # I modelli extra arrivano da un secret: un errore non ne stampa mai il testo.
            try:
                where, ident, regex = line.split(None, 2)
                if where not in ("content", "path", "magic"):
                    raise ValueError
                patterns.append((where, ident, re.compile(regex)))
            except (ValueError, re.error):
                sys.exit(f"Modello non valido fra quelli «{label}» (regola {number}).")
    return patterns


def pattern_counts():
    counts = {"kit": 0, "progetto": 0, "extra": 0}
    for label, path in pattern_files():
        counts[label] += len(read_rules(path))
    return counts


def load_allow():
    return [tuple(line.split(None, 1)) for line in read_rules(CONFIG_DIR / "allow")]


def allowed(path, ident, allow):
    return any(ident == a_id.strip() and fnmatch.fnmatchcase(path, glob) for glob, a_id in allow)


def read_blobs(shas):
    """sha -> contenuto, con un solo `git cat-file --batch`."""
    shas = sorted(set(shas))
    proc = subprocess.run(
        ["git", "cat-file", "--batch"],
        input="".join(f"{s}\n" for s in shas).encode(),
        capture_output=True,
        check=True,
    )
    data, pos, blobs = proc.stdout, 0, {}
    for sha in shas:
        header_end = data.index(b"\n", pos)
        size = int(data[pos:header_end].split()[2])
        start = header_end + 1
        blobs[sha] = data[start : start + size]
        pos = start + size + 1
    return blobs


def scan_text(label, text, patterns, allow, findings):
    for number, line in enumerate(text.splitlines(), 1):
        for where, ident, regex in patterns:
            if where == "content" and regex.search(line) and not allowed(label, ident, allow):
                findings.append((label, number, ident))


def scan_tree(entries, texts=()):
    """Trova i modelli nell'albero e nei testi (etichetta, testo). Mai i valori."""
    patterns, allow = load_patterns(), load_allow()
    findings = []
    blobs = read_blobs(s for _, k, s in entries.values() if k == "blob")
    for path, (_, kind, sha) in sorted(entries.items()):
        for where, ident, regex in patterns:
            if where == "path" and regex.search(path) and not allowed(path, ident, allow):
                findings.append((path, 0, ident))
        if kind != "blob":
            continue
        content = blobs[sha]
        if b"\0" in content[:8000]:
            head = content[:16].hex()
            for where, ident, regex in patterns:
                if where == "magic" and regex.search(head) and not allowed(path, ident, allow):
                    findings.append((path, 0, ident))
            continue
        scan_text(path, content.decode("utf-8", errors="replace"), patterns, allow, findings)
    for label, text in texts:
        scan_text(label, text, patterns, allow, findings)
    return findings


def findings_table(findings):
    rows = ["| File | Riga | Tipo |", "| :--- | ---: | :--- |"]
    for path, line, ident in findings:
        rows.append(f"| `{path}` | {line or '—'} | `{ident}` |")
    return "\n".join(rows)


# --- plan --------------------------------------------------------------------


def find_baseline(public_ref, head, fallback):
    synced = trailer_values(public_ref, SYNC_TRAILER)
    # Il più avanti nella storia privata, non il primo di `git log`: dopo un merge
    # commit l'ordine per data fra i due rami del pubblico non dice quale sync è l'ultimo.
    known = [(c, v) for c, v in synced if commit_exists(v)]
    if synced and not known:
        sys.exit(f"Nessuno dei commit privati indicati dai trailer {SYNC_TRAILER} esiste nel repo privato.")
    if known:
        public_commit, baseline = max(known, key=lambda cv: int(git("rev-list", "--count", cv[1]).strip()))
    elif fallback:
        public_commit, baseline = None, fallback
    else:
        sys.exit(
            f"Nel pubblico non c'è ancora un trailer {SYNC_TRAILER}: serve --baseline, il commit "
            "privato il cui albero è quello del commit iniziale del pubblico."
        )
    baseline = git("rev-parse", baseline).strip()
    if not is_ancestor(baseline, head):
        sys.exit(
            f"Il commit privato {baseline[:12]}, l'ultimo sincronizzato, non è un antenato di "
            f"{head[:12]}: la storia di main privato è stata riscritta. Va deciso a mano."
        )
    return baseline, public_commit


def included_commits(baseline, head, excludes):
    """(sha, soggetto) dei commit privati dopo baseline che toccano almeno un file pubblico."""
    out = git("log", "--reverse", "--no-merges", "--format=%H%x1f%s", f"{baseline}..{head}")
    commits = []
    for line in out.splitlines():
        sha, _, subject = line.partition("\x1f")
        files = git("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--root", sha).split("\0")
        if any(f and not is_excluded(f, excludes) for f in files):
            commits.append((sha, subject))
    return commits


def compose_message(subjects, head):
    count = len(subjects)
    title = f"Sincronizza dal repo privato ({count} commit)" if count else "Allinea al repo privato"
    body = "\n".join(f"- {s}" for s in subjects)
    trailer = f"{SYNC_TRAILER}: {head}"
    message = f"{title}\n\n{body}\n\n{trailer}\n" if body else f"{title}\n\n{trailer}\n"
    # Il trailer sta anche in fondo alla descrizione: con lo squash «titolo e
    # descrizione della PR» è da lì che GitHub prende il messaggio del commit.
    pr_body = (
        "Aggiornamento automatico dal repo di sviluppo: questo è l'albero di `main` meno "
        "i file privati. Si può unire con merge, squash o rebase: il trailer qui sotto "
        "dice al sync successivo da dove ripartire, e va lasciato in fondo al messaggio.\n\n"
        + (f"Commit inclusi:\n\n{body}\n\n" if body else "")
        + f"{trailer}\n"
    )
    return title, message, pr_body


def diff_status(old, new):
    changes = []
    for path in sorted(set(old) | set(new)):
        if path not in old:
            changes.append(("A", path))
        elif path not in new:
            changes.append(("D", path))
        elif old[path] != new[path]:
            changes.append(("M", path))
    return changes


def public_conflicts(baseline, head, public_ref, excludes):
    """File cambiati nel pubblico dopo l'ultimo sync che il sync sovrascriverebbe."""
    base = public_part(ls_tree(baseline), excludes)
    public = public_part(ls_tree(public_ref), excludes)
    target = public_part(ls_tree(head), excludes)
    drift = {p for p in set(base) | set(public) if base.get(p) != public.get(p)}
    accepted = {}
    for _, value in trailer_values(head, ACCEPT_TRAILER, f"{baseline}..{head}")[:1]:
        if commit_exists(value):
            accepted = public_part(ls_tree(value), excludes)
    conflicts = sorted(
        p
        for p in drift
        if target.get(p) != public.get(p) and not (accepted and accepted.get(p) == public.get(p))
    )
    return conflicts, write_tree(base) if conflicts else None


def public_only_commits(public_ref, last_sync_commit, paths):
    rev_range = f"{last_sync_commit}..{public_ref}" if last_sync_commit else public_ref
    synced = {c for c, _ in trailer_values(public_ref, SYNC_TRAILER, rev_range)}
    out = git("log", "--reverse", "--no-merges", "--format=%H%x1f%an%x1f%s", rev_range, "--", *paths)
    commits = []
    for line in out.splitlines():
        sha, author, subject = line.split("\x1f", 2)
        if sha not in synced:
            commits.append((sha, author, subject))
    return commits


def write_outputs(path, values):
    if not path:
        return
    with open(path, "a") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def plan(args):
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    excludes = load_excludes()
    # Anche quando non c'è niente da fare: un modello rotto si scopre subito.
    patterns = load_patterns()
    head = git("rev-parse", "--verify", f"{args.head}^{{commit}}").strip()
    public = git("rev-parse", "--verify", f"{args.public_ref}^{{commit}}").strip()
    baseline, last_sync_commit = find_baseline(public, head, args.baseline)
    short = git("rev-parse", "--short=7", head).strip()
    branch = f"{args.branch_prefix}{short}"

    public_entries = ls_tree(public)
    head_entries = ls_tree(head)
    entries = public_part(head_entries, excludes)
    entries.update({p: e for p, e in public_entries.items() if is_excluded(p, excludes)})
    tree = write_tree(entries)
    public_tree = git("rev-parse", f"{public}^{{tree}}").strip()

    counts = pattern_counts()
    summary = [
        f"## Sync verso il pubblico{' (prova a secco)' if args.dry_run else ''}",
        "",
        f"- privato: `{head[:12]}`, ultimo sincronizzato `{baseline[:12]}`"
        + ("" if last_sync_commit else " (fallback, nessun trailer nel pubblico)"),
        f"- pubblico: main a `{public[:12]}`",
        f"- esclusi dal privato: {sum(1 for p in head_entries if is_excluded(p, excludes))} file",
        f"- modelli dello scanner: {counts['kit']} del kit, {counts['progetto']} del progetto, "
        f"{counts['extra']} extra",
        "",
    ]
    outputs = {"branch": branch, "public_sha": public, "head_sha": head}

    def finish(result, code=0):
        outputs["result"] = result
        write_outputs(args.github_output, outputs)
        text = "\n".join(summary) + "\n"
        (out / "summary.md").write_text(text)
        if args.summary:
            with open(args.summary, "a") as handle:
                handle.write(text)
        print(text)
        return code

    if tree == public_tree:
        summary.append("**Niente da fare**: l'albero filtrato coincide con main del pubblico.")
        return finish("noop")

    changes = diff_status(public_part(public_entries, excludes), public_part(entries, excludes))
    summary.append(f"### File che cambiano nel pubblico ({len(changes)})\n")
    summary.append("```")
    summary.extend(f"{status} {path}" for status, path in changes[:200])
    if len(changes) > 200:
        summary.append(f"… e altri {len(changes) - 200}")
    summary.append("```\n")

    # Un soggetto resta per sempre nella storia privata: se contiene un modello, lo si
    # omette invece di bloccare, altrimenti ogni sync successivo si fermerebbe lì.
    subjects, omitted = [], []
    for sha, subject in included_commits(baseline, head, excludes):
        hits = []
        scan_text(sha, subject, patterns, [], hits)
        if hits:
            omitted.append((sha, sorted({ident for _, _, ident in hits})))
            subject = "(soggetto omesso)"
        subjects.append(subject)
    if omitted:
        summary.append("### ⚠️ Soggetti omessi dal messaggio\n")
        summary.extend(f"- `{sha[:12]}`: {', '.join(ids)}" for sha, ids in omitted)
        summary.append("")
    title, message, pr_body = compose_message(subjects, head)
    conflicts, base_tree = public_conflicts(baseline, head, public, excludes)
    findings = scan_tree(entries, [(MESSAGE_LABEL, message), (PR_BODY_LABEL, pr_body)])

    if findings:
        summary.append(f"### ❌ Scanner anti-fuga: {len(findings)} occorrenze\n")
        summary.append(findings_table(findings))
        summary.append(
            "\nIl valore non si stampa. Si toglie dal privato, o si aggiunge il percorso a "
            "`exclude`, o — se è davvero pubblicabile — un'eccezione motivata in `allow`.\n"
        )

    if conflicts:
        commits = public_only_commits(public, last_sync_commit, conflicts)
        (out / "public-net.diff").write_text(
            git("--literal-pathspecs", "diff", "--binary", base_tree, public, "--", *conflicts)
        )
        if commits:
            (out / "public-commits.patch").write_text(
                "".join(git("format-patch", "--stdout", "--binary", "-1", c) for c, _, _ in commits)
            )
        listing = "\n".join(f"- `{c[:12]}` {s} ({a})" for c, a, s in commits) or "- (nessun commit singolo: vedi il diff)"
        files = "\n".join(f"- `{p}`" for p in conflicts)
        run_url = ""
        if os.environ.get("GITHUB_RUN_ID"):
            run_url = (
                f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                f"{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
            )
        issue = (
            "Il sync verso il repo pubblico si è fermato: `main` del pubblico ha cambiamenti "
            "che non vengono da un sync, e la PR li cancellerebbe.\n\n"
            f"Commit del pubblico:\n\n{listing}\n\nFile:\n\n{files}\n\n"
            "Per riportarli qui:\n\n"
            + (f"1. scarica l'artefatto `public-sync` del run {run_url};\n" if run_url else "1. prendi i file dell'artefatto `public-sync`;\n")
            + "2. `git am public-commits.patch` (autori e messaggi dei commit) oppure "
            "`git apply public-net.diff` (solo il risultato), rivedi e committa su `main`;\n"
            "3. se il riporto non è identico al pubblico (li hai fusi con altro), aggiungi al "
            f"commit il trailer `{ACCEPT_TRAILER}: {public}`: dice al sync che quei "
            "cambiamenti si possono sovrascrivere.\n\n"
            "Il sync riparte da solo al push successivo, o dalla scheda Actions.\n"
        )
        (out / "issue.md").write_text(issue)
        summary.append(f"### ❌ Cambiamenti fatti nel pubblico ({len(conflicts)} file)\n")
        summary.append(listing + "\n\n" + files + "\n")
        summary.append("Patch nell'artefatto `public-sync`; istruzioni nella issue.\n")
        return finish("blocked-public", 1)

    if findings:
        return finish("blocked-leak", 1)

    for ref in git("for-each-ref", "--format=%(refname)", args.pr_prefix).split():
        if git("rev-parse", f"{ref}^{{tree}}").strip() == tree:
            outputs["branch"] = ref[len(args.pr_prefix) :]
            summary.append(f"**Già pronta**: la PR di `{outputs['branch']}` ha già questo albero.")
            return finish("pr-up-to-date")

    identity = {
        "GIT_AUTHOR_NAME": args.author_name,
        "GIT_AUTHOR_EMAIL": args.author_email,
        "GIT_COMMITTER_NAME": args.author_name,
        "GIT_COMMITTER_EMAIL": args.author_email,
    }
    commit = git("commit-tree", tree, "-p", public, "-F", "-", input=message.encode(), env=identity).strip()
    git("update-ref", f"refs/public-sync-out/{branch}", commit)
    (out / "message.txt").write_text(message)
    (out / "pr-title.txt").write_text(title + "\n")
    (out / "pr-body.md").write_text(pr_body)
    outputs["commit"] = commit
    summary.append(f"### Commit `{commit[:12]}` sul branch `{branch}`\n")
    summary.append("```\n" + message + "```")
    return finish("ready")


def scan(args):
    findings = scan_tree(ls_tree(args.treeish))
    if findings:
        print(findings_table(findings))
        return 1
    print(f"Scanner: nessuna occorrenza in {args.treeish}.")
    return 0


def main(argv=None):
    global CONFIG_DIR, EXTRA_PATTERNS, SYNC_TRAILER, ACCEPT_TRAILER
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config-dir", default=".github/public-sync", help="exclude, patterns e allow del progetto")
    common.add_argument(
        "--extra-patterns", action="append", default=[], metavar="FILE",
        help="altri modelli, nello stesso formato di patterns (ripetibile)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", parents=[common])
    p.add_argument("--head", default="HEAD")
    p.add_argument("--public-ref", default="refs/public/main")
    p.add_argument("--pr-prefix", default="refs/public-sync/", help="dove stanno i branch di sync già nel pubblico")
    p.add_argument("--branch-prefix", default="sync/", help="prefisso del branch di sync nel pubblico")
    p.add_argument("--baseline", default="", help="commit privato di partenza, finché il pubblico non ha trailer")
    p.add_argument("--sync-trailer", default="Private-Sync")
    p.add_argument("--accept-trailer", default="Public-Sync-Accept")
    p.add_argument("--author-name", default="github-actions[bot]")
    p.add_argument("--author-email", default="41898282+github-actions[bot]@users.noreply.github.com")
    p.add_argument("--out-dir", default="public-sync-out")
    p.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    p.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    p.add_argument("--dry-run", action="store_true", help="solo per il titolo del riepilogo")
    s = sub.add_parser("scan", parents=[common])
    s.add_argument("treeish")
    args = parser.parse_args(argv)
    CONFIG_DIR = Path(args.config_dir)
    EXTRA_PATTERNS = args.extra_patterns
    if args.command == "plan":
        SYNC_TRAILER, ACCEPT_TRAILER = args.sync_trailer, args.accept_trailer
        return plan(args)
    return scan(args)


if __name__ == "__main__":
    sys.exit(main())
