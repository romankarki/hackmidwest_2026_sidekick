"""Store the deploy secrets in both GitHub repos so Actions can deploy (run after provision.sh):

    GITHUB_TOKEN=<token with repo + workflow access> python deploy/set-github-secrets.py <server-ip>

Sets DEPLOY_HOST, DEPLOY_SSH_KEY, DEPLOY_KNOWN_HOSTS (+ PREVIEW_DOMAIN on shoplite-target). Secrets are encrypted
client-side with each repo's public key (libsodium sealed box), as the GitHub API requires. Needs: pip install pynacl
"""
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

from nacl import encoding, public

REPOS = {"romankarki/shoplite-target": True, "romankarki/hackmidwest_2026_sidekick": False}  # repo → needs PREVIEW_DOMAIN
KEYS = Path(__file__).parent / ".keys"


def gh(method, path, body=None):
    req = urllib.request.Request(f"https://api.github.com{path}", method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def put_secret(repo, name, value, key):
    box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
    gh("PUT", f"/repos/{repo}/actions/secrets/{name}",
       {"encrypted_value": base64.b64encode(box.encrypt(value.encode())).decode(), "key_id": key["key_id"]})
    print(f"  ✓ {repo}: {name}")


def main(ip):
    values = {"DEPLOY_HOST": ip, "DEPLOY_SSH_KEY": (KEYS / "ci_deploy_key").read_text(),
              "DEPLOY_KNOWN_HOSTS": (KEYS / "known_hosts").read_text().strip()}
    for repo, previews in REPOS.items():
        key = gh("GET", f"/repos/{repo}/actions/secrets/public-key")
        for name, value in values.items():
            put_secret(repo, name, value, key)
        if previews:  # PR previews run on LKE once deploy/lke-setup.sh has run, else on the VM
            k8s_host = KEYS / "k8s-host"
            put_secret(repo, "PREVIEW_DOMAIN", k8s_host.read_text().strip() if k8s_host.exists() else f"{ip.replace('.', '-')}.sslip.io", key)


if __name__ == "__main__":
    main(sys.argv[1])
