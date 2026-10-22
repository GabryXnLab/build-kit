import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pick_runner as pr  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
ALL = {"nexus", "ohos", "ubicloud"}


def fake_api(name):
    """Serve le risposte dell'API GitHub registrate in fixtures/<name>.json."""
    with open(os.path.join(FIX, name + ".json")) as f:
        data = json.load(f)

    def get(path):
        if path.startswith("/orgs/"):
            return data["runners"]
        parts = path.split("?")[0].split("/")  # /repos/org/repo/actions/runs[/id/jobs]
        repo = data["repos"].get(parts[3], {})
        if parts[-1] == "jobs":
            run_id = int(parts[-2])
            for runs in repo.values():
                for run in runs:
                    if run["id"] == run_id:
                        return {"jobs": run["jobs"]}
            return {"jobs": []}
        status = path.split("status=")[1].split("&")[0]
        return {"workflow_runs": [{"id": r["id"]} for r in repo.get(status, [])]}

    return get


def run(name, allow=ALL, arch="x64", target="any", repos=("kagami", "ascend"), repo=None, listed=("ascend", "Riftgate")):
    res, _ = pr.pick(fake_api(name), "GabryXnLab", arch, "8", list(repos), set(allow), target, repo, listed)
    return res


def attempts_api(jobs):
    def get(path):
        assert "/attempts/1/jobs" in path, path
        return {"jobs": jobs}

    return get


class OhosStuck(unittest.TestCase):
    cancelled = {"conclusion": "cancelled", "runner_id": 0, "labels": ["self-hosted", "ohos"]}

    def stuck(self, jobs, attempt=2, label="ohos"):
        return pr.ohos_stuck_before(attempts_api(jobs), "GabryXnLab/ascend", "1", attempt, label)

    def test_primo_tentativo_mai_escluso(self):
        self.assertFalse(pr.ohos_stuck_before(lambda p: self.fail(p), "o/r", "1", 1))

    def test_annullato_in_coda_su_ohos(self):
        self.assertTrue(self.stuck([self.cancelled]))

    def test_annullato_dopo_essere_partito(self):
        self.assertFalse(self.stuck([{**self.cancelled, "runner_id": 9}]))

    def test_annullato_su_altra_etichetta(self):
        self.assertFalse(self.stuck([{**self.cancelled, "labels": ["self-hosted", "nexus-core"]}]))

    def test_fallito_non_annullato(self):
        self.assertFalse(self.stuck([{**self.cancelled, "conclusion": "failure"}]))

    def test_etichetta_configurabile(self):
        self.assertTrue(self.stuck([{**self.cancelled, "labels": ["x"]}], label="x"))


class PickRunner(unittest.TestCase):
    def test_nexus_libero(self):
        r = run("nexus_libero")
        self.assertEqual((r["profile"], json.loads(r["runs_on"]), r["arch"]), ("nexus", ["self-hosted", "nexus-core"], "arm64"))

    def test_nexus_occupato_ohos_libero(self):
        r = run("nexus_occupato_ohos_libero")
        self.assertEqual((r["profile"], json.loads(r["runs_on"]), r["arch"]), ("ohos", ["self-hosted", "ohos"], "x64"))

    def test_entrambi_occupati(self):
        r = run("entrambi_occupati")
        self.assertEqual((r["profile"], json.loads(r["runs_on"])), ("ephemeral", ["ubicloud-standard-8"]))

    def test_ohos_offline(self):
        self.assertEqual(run("ohos_offline")["profile"], "ephemeral")

    def test_coda_satura_nexus(self):
        # due runner liberi, due job in coda su nexus-core: il terzo va a ohos
        self.assertEqual(run("coda_satura_nexus")["profile"], "ohos")

    def test_coda_satura_nexus_e_ohos(self):
        self.assertEqual(run("coda_satura_nexus_e_ohos")["profile"], "ephemeral")

    def test_coda_conta_run_in_corso_una_volta(self):
        q = pr.queued_per_label(fake_api("coda_satura_nexus_e_ohos"), "GabryXnLab", ["ascend"])
        self.assertEqual(q.get("ohos"), 2)

    def test_arm64_ubicloud(self):
        r = run("entrambi_occupati", arch="arm64")
        self.assertEqual((json.loads(r["runs_on"]), r["arch"]), (["ubicloud-standard-8-arm"], "arm64"))

    def test_target_x64_esclude_nexus(self):
        r = run("nexus_libero", target="x64")
        self.assertEqual(r["profile"], "ohos")

    def test_target_arm64_esclude_ohos(self):
        r = run("nexus_occupato_ohos_libero", target="arm64")
        self.assertEqual((json.loads(r["runs_on"]), r["arch"]), (["ubicloud-standard-8-arm"], "arm64"))

    def test_target_vince_su_arch(self):
        r = run("entrambi_occupati", arch="x64", target="arm64")
        self.assertEqual(json.loads(r["runs_on"]), ["ubicloud-standard-8-arm"])

    def test_token_mancante(self):
        res, _ = pr.pick(None, "GabryXnLab", "x64", "8", [], ALL, "any")
        self.assertEqual(res["profile"], "nexus")

    def test_token_mancante_target_x64(self):
        res, _ = pr.pick(None, "GabryXnLab", "x64", "8", [], ALL, "x64")
        self.assertEqual(res["profile"], "ephemeral")

    def test_errore_api(self):
        def boom(path):
            raise urllib.error.URLError("giù")
        res, _ = pr.pick(boom, "GabryXnLab", "x64", "8", ["kagami"], ALL, "any")
        self.assertEqual(res["profile"], "nexus")

    def test_allow_solo_nexus_occupato(self):
        r = run("entrambi_occupati", allow={"nexus"})
        self.assertEqual(r["profile"], "nexus")

    def test_allow_solo_nexus_libero(self):
        self.assertEqual(run("nexus_libero", allow={"nexus"})["profile"], "nexus")

    def test_allow_senza_ubicloud(self):
        self.assertEqual(run("entrambi_occupati", allow={"nexus", "ohos"})["profile"], "nexus")

    def test_repo_in_elenco_usa_ubicloud(self):
        self.assertEqual(run("entrambi_occupati", repo="GabryXnLab/ascend")["profile"], "ephemeral")

    def test_repo_fuori_elenco_usa_ohos(self):
        self.assertEqual(run("nexus_occupato_ohos_libero", repo="GabryXnLab/altro")["profile"], "ohos")

    def test_repo_fuori_elenco_tutto_pieno_coda_su_nexus(self):
        r = run("entrambi_occupati", repo="GabryXnLab/altro")
        self.assertEqual((r["profile"], json.loads(r["runs_on"])), ("nexus", ["self-hosted", "nexus-core"]))

    def test_elenco_file_ignora_commenti(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as f:
            f.write("# c\nascend  # x\n\nRiftgate\n")
            f.flush()
            self.assertEqual(pr.read_repo_list(f.name), ["ascend", "Riftgate"])

    def test_elenco_reale_ha_i_tre_repo(self):
        listed = pr.read_repo_list(os.path.join(os.path.dirname(pr.__file__), "ubicloud-repos.txt"))
        self.assertEqual(sorted(listed), ["Riftgate", "ascend", "kagami-private"])

    def test_main_input_non_validi(self):
        os.environ["PICK_ALLOW"] = "nexus,boh"
        try:
            self.assertEqual(pr.main(), 1)
        finally:
            del os.environ["PICK_ALLOW"]


if __name__ == "__main__":
    unittest.main()
