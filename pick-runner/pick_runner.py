#!/usr/bin/env python3
"""Sceglie dove far girare una build: nexus-core, ohos o un runner effimero Ubicloud.

Solo libreria standard. Gli input arrivano da variabili d'ambiente PICK_* (le imposta
action.yml); fuori da GitHub Actions stampa gli output su stdout.
"""
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
ORDER = ("nexus", "ohos", "ubicloud")
# Architettura nativa dei runner self-hosted: serve per le build che producono un binario
# dell'architettura dell'host (desktop), non per quelle che girano ovunque (Android).
NATIVE_ARCH = {"nexus": "arm64", "ohos": "x64"}
LABEL = {"nexus": "nexus-core", "ohos": "ohos"}
RUNS_ON = {"nexus": ["self-hosted", "nexus-core"], "ohos": ["self-hosted", "ohos"]}


def http_get(path, token):
    req = urllib.request.Request(
        API + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def free_runners(runners, label):
    return sum(
        1
        for r in runners
        if r.get("status") == "online"
        and not r.get("busy")
        and label in {lb["name"] for lb in r.get("labels", [])}
    )


def queued_per_label(get, org, repos):
    """Conta i job `queued` per etichetta nei repo dati (run in coda o in corso)."""
    counts = {}
    for repo in repos:
        full = repo if "/" in repo else f"{org}/{repo}"
        seen = set()
        for status in ("queued", "in_progress"):
            runs = get(f"/repos/{full}/actions/runs?status={status}&per_page=20")
            for run in runs.get("workflow_runs", []):
                if run["id"] in seen:
                    continue
                seen.add(run["id"])
                jobs = get(f"/repos/{full}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100")
                for job in jobs.get("jobs", []):
                    if job.get("status") == "queued":
                        for lb in job.get("labels", []):
                            counts[lb] = counts.get(lb, 0) + 1
    return counts


def eligible_levels(allow, target_arch):
    levels = []
    for level in ORDER:
        if level not in allow:
            continue
        if target_arch != "any" and level in NATIVE_ARCH and NATIVE_ARCH[level] != target_arch:
            continue
        levels.append(level)
    return levels


def ubicloud_label(size, arch):
    return f"ubicloud-standard-{size}" + ("-arm" if arch == "arm64" else "")


def result(level, arch, size, reason):
    if level == "ubicloud":
        runs_on, out_arch = [ubicloud_label(size, arch)], arch
    else:
        runs_on, out_arch = RUNS_ON[level], NATIVE_ARCH[level]
    return {"runs_on": json.dumps(runs_on), "profile": "ephemeral" if level == "ubicloud" else level,
            "arch": out_arch, "reason": reason}


def fallback(levels, arch, size, why):
    # Il comportamento di prima di `auto` è nexus; se l'architettura lo esclude, la scelta
    # che non dipende da una macchina accesa è Ubicloud.
    for level in ("nexus", "ubicloud", "ohos"):
        if level in levels:
            print(f"::warning::pick-runner: {why}; ripiego su {level}")
            return result(level, arch, size, f"{why}: ripiego su {level}")
    print(f"::warning::pick-runner: {why} e nessun livello ammesso; ripiego su nexus")
    return result("nexus", arch, size, f"{why}: nessun livello ammesso, ripiego su nexus")


def pick(get, org, arch, size, repos, allow, target_arch):
    if target_arch != "any":
        arch = target_arch
    levels = eligible_levels(allow, target_arch)
    if get is None:
        return fallback(levels, arch, size, "token mancante"), []
    try:
        runners = get(f"/orgs/{org}/actions/runners?per_page=100").get("runners", [])
        queued = queued_per_label(get, org, repos)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        return fallback(levels, arch, size, f"API non raggiungibile ({exc})"), []

    table = []
    for level in levels:
        if level == "ubicloud":
            table.append((level, None, None, None))
            continue
        free = free_runners(runners, LABEL[level])
        wait = queued.get(LABEL[level], 0)
        table.append((level, free, wait, free - wait))
    for level, _, _, cap in table:
        if level == "ubicloud":
            return result(level, arch, size, "nexus e ohos senza capacità libera: runner effimero"
                          if len(table) > 1 else "solo ubicloud ammesso"), table
        if cap > 0:
            return result(level, arch, size, f"{level} con capacità libera {cap}"), table
    # Nessun livello libero e Ubicloud non ammesso: si accoda sul primo, come oggi.
    first = table[0][0] if table else "nexus"
    return result(first, arch, size, f"nessuna capacità libera e ubicloud non ammesso: coda su {first}"), table


def summary(res, table, target_arch):
    lines = ["### Scelta del runner", f"**{res['profile']}** (`{res['runs_on']}`, {res['arch']}): {res['reason']}", ""]
    if table:
        lines += ["| livello | runner liberi | job in coda | capacità |", "| --- | --- | --- | --- |"]
        for level, free, wait, cap in table:
            lines.append(f"| {level} | {'-' if free is None else free} | {'-' if wait is None else wait} | {'-' if cap is None else cap} |")
    if target_arch != "any":
        lines.append(f"\nArchitettura richiesta: {target_arch}")
    return "\n".join(lines) + "\n"


def main():
    env = os.environ.get
    token = env("PICK_TOKEN", "")
    allow = {a.strip() for a in env("PICK_ALLOW", "nexus,ohos,ubicloud").split(",") if a.strip()}
    repos = [r.strip() for r in env("PICK_REPOS", "").replace("\n", ",").split(",") if r.strip()]
    arch = env("PICK_ARCH", "x64")
    target_arch = env("PICK_TARGET_ARCH", "any")
    if arch not in ("x64", "arm64") or target_arch not in ("any", "x64", "arm64") or not allow <= set(ORDER):
        print("::error::pick-runner: input non validi (arch x64|arm64, target_arch any|x64|arm64, allow tra nexus,ohos,ubicloud)")
        return 1
    if env("PICK_JITTER", "true") == "true":
        time.sleep(random.uniform(0, 15))
    get = (lambda p: http_get(p, token)) if token else None
    res, table = pick(get, env("PICK_ORG", "GabryXnLab"), arch, env("PICK_UBICLOUD_SIZE", "8"), repos, allow, target_arch)

    print(f"pick-runner: {res['reason']} -> {res['runs_on']}")
    out = env("GITHUB_OUTPUT")
    text = "".join(f"{k}={v}\n" for k, v in res.items())
    if out:
        with open(out, "a") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    summ = env("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a") as f:
            f.write(summary(res, table, target_arch))
    return 0


if __name__ == "__main__":
    sys.exit(main())
