#!/usr/bin/env python3
"""Riavvia il server sccache degli agenti (gx-sccache, porta 4227) quando è bloccato.

sccache 0.18 può perdere i token del suo jobserver interno (visto il 2026-10-23 dopo client uccisi a metà
compilazione): il server accetta ancora le connessioni ma ogni compilazione aspetta un token che non
arriva, e i client restano fermi per ore a CPU 0. Bloccato = fra due passaggi del timer (10 min) stesso
PID, client rustc in attesa, nessun processo figlio e tempo di CPU fermo. Ucciso il server, il client
successivo ne avvia uno nuovo; i client già appesi falliscono e cargo va rilanciato.
"""
import json, os, signal, time

PORT = os.environ.get("GX_SCCACHE_PORT", "4227")
STATE = os.path.expanduser("~/.cache/sccache-locale/sorveglia.json")


def procs():
    out = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")[:-1]
            with open(f"/proc/{pid}/stat") as f:
                st = f.read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if argv and os.path.basename(argv[0]) == b"sccache":
            out.append((int(pid), argv, int(st[1]), int(st[11]) + int(st[12])))
    return out


def server_port(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            env = f.read().split(b"\0")
    except OSError:
        return None
    return next((e.split(b"=", 1)[1].decode() for e in env if e.startswith(b"SCCACHE_SERVER_PORT=")), "4226")


def main():
    ps = procs()
    server = next(((p, cpu) for p, argv, _, cpu in ps if len(argv) == 1 and server_port(p) == PORT), None)
    if server is None:
        return
    pid, cpu = server
    clients = sum(1 for _, argv, _, _ in ps if len(argv) > 1 and os.path.basename(argv[1]).startswith(b"rustc"))
    children = sum(1 for d in filter(str.isdigit, os.listdir("/proc")) if _ppid(d) == pid)
    try:
        with open(STATE) as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = {}
    stuck = clients and not children and prev.get("pid") == pid and prev.get("cpu") == cpu
    if stuck:
        print(f"sorveglia-sccache: server {pid} fermo con {clients} client in attesa, lo riavvio")
        os.kill(pid, signal.SIGTERM)
        time.sleep(5)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        prev = {}
    else:
        prev = {"pid": pid, "cpu": cpu} if clients and not children else {}
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(prev, f)


def _ppid(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


if __name__ == "__main__":
    main()
