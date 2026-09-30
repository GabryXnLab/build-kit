#!/usr/bin/env python3
"""Prove di sync.py su repo git finti: python3 -B -m unittest public-sync/test_sync.py

Solo libreria standard e git. Ogni prova crea un repo privato in una cartella
temporanea, con main del pubblico in refs/public/main come dopo il fetch del workflow.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SYNC = Path(__file__).resolve().parent / "sync.py"
ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Dev",
    "GIT_AUTHOR_EMAIL": "dev@example.com",
    "GIT_COMMITTER_NAME": "Dev",
    "GIT_COMMITTER_EMAIL": "dev@example.com",
}
ENV.pop("GITHUB_OUTPUT", None)
ENV.pop("GITHUB_STEP_SUMMARY", None)

EXCLUDE = ".github/public-sync/\n.github/workflows/public-sync.yml\nNOTES.local.md\n"
PATTERNS = "content project-host (?i)casa-mia\\.lan\n"


class SyncTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "private"
        self.out = Path(self._tmp.name) / "out"
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=self._tmp.name)
        self.write(".github/public-sync/exclude", EXCLUDE)
        self.write(".github/public-sync/patterns", PATTERNS)
        self.write(".github/public-sync/allow", "")
        self.write("README.md", "App\n")
        self.write("NOTES.local.md", "note private\n")
        self.baseline = self.commit("Primo commit")
        # Il pubblico nasce con l'albero del privato meno gli esclusi, senza trailer.
        self.git("rm", "-q", "--cached", "-r", ".github", "NOTES.local.md")
        tree = self.git("write-tree")
        self.git("reset", "-q")
        public = self.git("commit-tree", tree, "-m", "App")
        self.git("update-ref", "refs/public/main", public)

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args, cwd=None, input=None):
        result = subprocess.run(
            ["git", *args], cwd=cwd or self.repo, env=ENV, input=input,
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, subject):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def sync(self, *args, extra=None):
        cmd = [sys.executable, "-B", str(SYNC), "plan", "--out-dir", str(self.out),
               "--github-output", str(self.out / "outputs"), "--baseline", self.baseline, *args]
        if extra is not None:
            path = Path(self._tmp.name) / "extra"
            path.write_text(extra)
            cmd += ["--extra-patterns", str(path)]
        self.out.mkdir(exist_ok=True)
        (self.out / "outputs").write_text("")
        proc = subprocess.run(cmd, cwd=self.repo, env=ENV, capture_output=True, text=True)
        outputs = dict(line.split("=", 1) for line in (self.out / "outputs").read_text().splitlines())
        return proc, outputs

    def test_noop(self):
        proc, outputs = self.sync()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(outputs["result"], "noop")

    def test_ready_filters_private_files_and_adds_trailer(self):
        self.write("src/main.txt", "codice\n")
        self.write("NOTES.local.md", "altre note\n")
        self.commit("Aggiunge il codice")
        self.write(".github/public-sync/allow", "# niente\n")
        head = self.commit("Solo configurazione privata")
        proc, outputs = self.sync("--branch-prefix", "mirror/", "--sync-trailer", "From-Private")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(outputs["result"], "ready")
        self.assertEqual(outputs["branch"], f"mirror/{head[:7]}")
        commit = outputs["commit"]
        files = self.git("ls-tree", "-r", "--name-only", commit).splitlines()
        self.assertEqual(sorted(files), ["README.md", "src/main.txt"])
        self.assertEqual(self.git("rev-list", "--parents", "-n1", commit).split()[1:],
                         [self.git("rev-parse", "refs/public/main")])
        message = self.git("log", "-1", "--format=%B", commit)
        # Solo i commit che toccano file pubblici finiscono nel messaggio.
        self.assertIn("- Aggiunge il codice", message)
        self.assertNotIn("Solo configurazione privata", message)
        self.assertTrue(message.endswith(f"From-Private: {head}"))
        self.assertIn(f"From-Private: {head}", (self.out / "pr-body.md").read_text())

    def test_next_sync_starts_from_trailer(self):
        self.write("a.txt", "a\n")
        self.commit("Primo sync")
        _, outputs = self.sync()
        # Il pubblico unisce la PR: il sync seguente riparte dal trailer, non dalla baseline.
        self.git("update-ref", "refs/public/main", outputs["commit"])
        self.write("b.txt", "b\n")
        self.commit("Secondo sync")
        proc, outputs = self.sync("--baseline", "")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        message = self.git("log", "-1", "--format=%B", outputs["commit"])
        self.assertIn("- Secondo sync", message)
        self.assertNotIn("Primo sync", message)

    def test_missing_baseline_without_trailer(self):
        self.write("a.txt", "a\n")
        self.commit("Qualcosa")
        proc, _ = self.sync("--baseline", "")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("serve --baseline", proc.stderr)

    def test_scanner_blocks_kit_project_and_extra_patterns(self):
        self.write("config.txt", "host casa-mia.lan\nutente mario-rossi\n")
        self.write("app/google-services.json", "{}\n")
        self.commit("Configurazione")
        proc, outputs = self.sync(extra="content personal-name (?i)mario-rossi\n")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(outputs["result"], "blocked-leak")
        summary = (self.out / "summary.md").read_text()
        for ident in ("project-host", "personal-name", "google-services"):
            self.assertIn(f"`{ident}`", summary)
        # Il valore trovato non si stampa mai.
        self.assertNotIn("mario-rossi", summary)
        self.assertNotIn("mario-rossi", proc.stdout)

    def test_allow_and_omitted_subject(self):
        self.write("docs/hosts.md", "casa-mia.lan\n")
        self.write(".github/public-sync/allow", "docs/hosts.md project-host\n")
        self.commit("Documenta casa-mia.lan")
        proc, outputs = self.sync()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        message = self.git("log", "-1", "--format=%B", outputs["commit"])
        self.assertIn("(soggetto omesso)", message)
        self.assertNotIn("casa-mia", message)

    def test_invalid_extra_pattern_is_not_printed(self):
        proc, _ = self.sync(extra="content segreto-rotto (non-chiusa\n")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("extra", proc.stderr)
        self.assertNotIn("non-chiusa", proc.stderr + proc.stdout)

    def test_public_contribution_blocks_and_accept_trailer_releases(self):
        # Un commit fatto direttamente nel pubblico.
        public = self.git("rev-parse", "refs/public/main")
        blob = self.git("hash-object", "-w", "--stdin", input="App corretta\n")
        tree = self.git("mktree", input=f"100644 blob {blob}\tREADME.md\n")
        contrib = self.git("commit-tree", tree, "-p", public, "-m", "Correzione")
        self.git("update-ref", "refs/public/main", contrib)
        self.write("b.txt", "b\n")
        self.commit("Altro")
        proc, outputs = self.sync()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(outputs["result"], "blocked-public")
        self.assertIn("README.md", (self.out / "issue.md").read_text())
        self.assertTrue((self.out / "public-commits.patch").is_file())
        # Riportato nel privato, fuso con altro: il trailer dice che si può sovrascrivere.
        self.write("README.md", "App corretta, e poi rivista\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"Riporta la correzione\n\nPublic-Sync-Accept: {contrib}")
        proc, outputs = self.sync()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(outputs["result"], "ready")


if __name__ == "__main__":
    unittest.main()
