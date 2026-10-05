"""Guard: nothing from your own clusters or brand may appear in a file git would commit.

The sensitive terms are read from the git-ignored local data that Lex keeps (data/ and brand/), so they never
appear in this file: cluster and context names, AWS account IDs, image registries, your apps' names and
namespaces, and your brand's name and website. The test is skipped where there is no local data (fresh clones, CI).
Run with: python3 -m unittest tests.test_no_company_data
"""

import glob
import json
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import archipelago  # noqa: E402
import parse_cluster  # noqa: E402

BINARY = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".csv", ".db")
# Well-known open-source product names that may also run as apps in a cluster; Lex names them on purpose
GENERIC = {"gitlab-runner", "actions-runner", "argo-workflows", "sonarqube", "sonarqube-postgresql", "milvus-operator"}


def sensitive_terms():
    terms = set()
    for path in glob.glob(os.path.join(parse_cluster.DATA_DIR, "cluster_state-*.json")):
        ctx = os.path.basename(path)[len("cluster_state-"):-len(".json")]
        terms.add(ctx.split("_cluster_")[-1])   # the cluster's own name (EKS contexts end in cluster/<name>)
        terms.update(re.findall(r"(?<!\d)\d{12}(?!\d)", ctx))                          # AWS account IDs
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            continue
        summary = archipelago.summarize(state)
        cfg = archipelago.load_config(None)
        for key, w in summary["workloads"].items():
            if archipelago.hidden_reason(key, w, set(), cfg) is None:   # an app (platform and tooling are generic)
                terms.add(w["name"])
                terms.add(w["namespace"])
        for n in state.get("nodes") or []:
            for p in n.get("pods") or []:
                for c in p.get("containers") or []:
                    registry = (c.get("image") or "").split("/")[0]
                    if ".dkr.ecr." in registry or re.search(r"\d{12}", registry):
                        terms.add(registry)
    brand_file = os.path.join(ROOT, "brand", "brand.json")
    if os.path.exists(brand_file):
        try:
            with open(brand_file, encoding="utf-8") as f:
                b = json.load(f)
            terms.add(b.get("name") or "")
            host = re.sub(r"^https?://(www\.)?", "", b.get("url") or "").split("/")[0]
            terms.add(host)
            if b.get("accentColor"):
                terms.add(b["accentColor"])
        except (OSError, ValueError):
            pass
    # Short or single-word names ("api", "web") are too generic to mean anything
    return {t for t in terms if t and len(t) >= 5 and t.lower() not in GENERIC and (re.search(r"[-_.\d#]", t) or " " in t)}


def committable_files():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return [f for f in out.splitlines() if f and not f.lower().endswith(BINARY) and os.path.isfile(os.path.join(ROOT, f))]


class NoCompanyDataTest(unittest.TestCase):
    def test_no_local_identifiers_in_committable_files(self):
        terms = sensitive_terms()
        if not terms:
            self.skipTest("no local cluster or brand data to check against")
        patterns = [(t, re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(t) + r"(?![A-Za-z0-9_])", re.I)) for t in terms]
        leaks = {}
        for f in committable_files():
            with open(os.path.join(ROOT, f), encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
            found = sorted({t for t, rx in patterns if rx.search(text)})
            if found:
                leaks[f] = found[:10]
        self.assertEqual(leaks, {}, "company-specific names in files git would commit; replace them with placeholders "
                                    "or git-ignore the file")


if __name__ == "__main__":
    unittest.main()
