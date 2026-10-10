"""Kubernetes (LKE) PR previews: list them and the kill switch (delete one / delete all).

Sidekick never holds cluster credentials. It SSHes to the VM's gate with a key whose authorized_keys entry forces
`gate.sh dashboard`, which only allows `kpreview ls | kpreview down <pr> | kpreview down-all` (see deploy/gate.sh).

Env (set on the server by deploy/bootstrap.sh; unset locally → "not configured"):
  GATE_SSH_KEY   base64 of the dashboard gate private key
  GATE_HOST_KEY  the VM's ssh host public key ("ssh-ed25519 AAAA…"), pinned
  GATE_HOST      default host.docker.internal (the VM itself, from inside the container)
"""
import base64
import json
import os
import subprocess
import tempfile


def configured():
    return bool(os.getenv("GATE_SSH_KEY") and os.getenv("GATE_HOST_KEY"))


def _gate(command, timeout=120):
    if not configured():
        raise RuntimeError("Previews aren't configured here (no gate key). They run on the hosted Sidekick.")
    host = os.getenv("GATE_HOST", "host.docker.internal")
    with tempfile.TemporaryDirectory() as d:
        key, known = os.path.join(d, "key"), os.path.join(d, "known_hosts")
        with open(key, "wb") as f:
            f.write(base64.b64decode(os.environ["GATE_SSH_KEY"]))
        os.chmod(key, 0o600)
        with open(known, "w") as f:
            f.write(f"{host} {os.environ['GATE_HOST_KEY'].strip()}\n")
        r = subprocess.run(["ssh", "-i", key, "-o", f"UserKnownHostsFile={known}", "-o", "StrictHostKeyChecking=yes",
                            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10", f"deploy@{host}", command],
                           capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stdout + r.stderr).strip()[-400:] or f"gate exited {r.returncode}")
    return r.stdout


def list_previews():
    return json.loads(_gate("kpreview ls") or "[]")


def delete(pr: int):
    _gate(f"kpreview down {int(pr)}")


def delete_all():
    n = len(list_previews())
    _gate("kpreview down-all", timeout=300)
    return n
