"""F-36 Archipelago: other clusters as islands next to the one on screen, and which *apps* run different versions.

Only applications are compared, and only between environment siblings (shop-integ vs shop-prod).
Everything a cluster runs just because it is a cluster is left out of the comparison:
  - platform: node agents (DaemonSets), system and tooling namespaces (Lex's platform list plus observability, GitOps,
    secrets, autoscaling, cost...), and ingress controllers / gateways, which every cluster needs whatever it serves
  - fleet standards: the same Kind/name running in at least half the clusters Lex knows (datadog, flux, coredns...)
  - batch (Jobs), idle (scaled to zero, no pods) and ephemeral workloads (per-PR previews named with a UUID)
A version is the image tag, reduced to its commit when it has one (build-dev-3f9a2c1-1787852932 and
build-prod-3f9a2c1-1787900000 are the same build), else its semver or the tag without environment words.

Pure functions over compiled cluster states (no kubectl, no I/O) so they are cheap to unit test.
"""

import copy
import fnmatch
import math
import re

import security

DEMO_ISLAND = "demo-staging"
MAX_ISLANDS = 4
FLEET_SHARE = 0.5          # in at least this share of known clusters (and at least 3) = a fleet standard
FLEET_MIN_CLUSTERS = 3

# Pod statuses drawn as failing rooms on an island
_FAILING = re.compile(r"BackOff|Err|Error|Failed|OOMKilled|Unknown|CreateContainer", re.I)

# Words that name an environment rather than a system ("shop-prod" and "shop-integ" are siblings)
_ENV_WORDS = {"prod", "production", "prd", "integ", "integration", "int", "stage", "staging", "stg", "dev", "develop",
              "development", "test", "testing", "qa", "uat", "preprod", "sandbox", "sbx", "perf", "canary", "demo"}

# Namespaces of shared tooling, on top of security.py's platform list (kube-system, istio-*, linkerd*, cert-manager...)
PLATFORM_NAMESPACE_PREFIXES = ("kube-", "datadog", "fluent", "flux-system", "argocd", "argo-", "external-secrets",
                               "sealed-secrets", "vault", "falcon", "crowdstrike", "metrics-server", "autoscaler",
                               "cluster-autoscaler", "keda", "kubecost", "opencost", "knative-", "monitoring",
                               "prometheus", "grafana", "loki", "tempo", "opentelemetry", "otel", "velero", "crossplane",
                               "tekton", "gitlab-runner", "actions-runner", "aws-", "amazon-", "gke-", "azure-")
# Ingress controllers and gateways: required by every cluster whatever it serves, so never "the same app"
_INGRESS_WORDS = {"ingress", "ingressgateway", "egressgateway", "gateway"}
_INGRESS_IMAGE = re.compile(r"ingress-nginx|nginx-ingress|nginx-gateway|gateway-fabric|istio/proxyv2|proxyv2|istio-proxy|"
                            r"envoyproxy|envoy-gateway|traefik|kong|haproxy|contour|emissary|ambassador|"
                            r"aws-load-balancer-controller|skipper", re.I)
# Service-mesh sidecars injected into pods: not part of the app's version (spec images never contain them)
_MESH_SIDECAR = re.compile(r"istio/proxyv2|/proxyv2:|istio-proxy|linkerd/proxy|l5d\.io/linkerd/proxy|consul-dataplane|kuma-dp", re.I)
_EPHEMERAL = re.compile(r"[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX = re.compile(r"^[0-9a-f]{7,40}$")
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?")

HIDDEN_REASONS = {
    "platform": "Platform and node agents",
    "ingress": "Ingress controllers and gateways",
    "fleet": "Fleet standards (in most clusters)",
    "batch": "Jobs",
    "idle": "Scaled to zero",
    "ephemeral": "Ephemeral (per-PR previews)",
    "config": "Ignored in archipelago.json",
}


def _strip_registry(image):
    """'1111.dkr.ecr.../team/api:1.4' -> 'team/api:1.4' (the first segment is a registry if it has a dot, colon or is localhost)."""
    first, sep, rest = (image or "").partition("/")
    return rest if sep and ("." in first or ":" in first or first == "localhost") else (image or "")


def normalize_image(image):
    """Last path segment plus tag (or a short digest): registries differ per environment (one ECR per account)."""
    if not image:
        return ""
    ref, _, digest = image.partition("@")
    last = ref.rsplit("/", 1)[-1]
    name, sep, tag = last.partition(":")
    if sep:
        return f"{name}:{tag}"
    if digest:
        return f"{name}@{digest.split(':')[-1][:12]}"
    return f"{name}:latest"


def version_of(image):
    """What a tag says about the build: {name, tag, key, label, commit, built, semver}.

    key decides sameness: the commit (7 hex) when the tag carries one, else the tag with environment words removed.
    built is a Unix timestamp found in the tag (CI build time), used to say which side is newer.
    """
    norm = normalize_image(image)
    if "@" in norm and ":" not in norm:
        name, _, digest = norm.partition("@")
        return {"name": name, "tag": "@" + digest, "key": "digest:" + digest, "label": digest[:7],
                "commit": None, "built": None, "semver": None}
    name, _, tag = norm.partition(":")
    tokens = [t for t in re.split(r"[-_.+]", tag.lower()) if t]
    built = next((int(t) for t in tokens if t.isdigit() and len(t) == 10 and 1_400_000_000 < int(t) < 2_200_000_000), None)
    # A short SHA can be all digits (7654321); a 10-digit build timestamp is not a commit
    commit = next((t for t in tokens if _HEX.match(t) and not (built and t == str(built))), None)
    m = _SEMVER.match(tag)
    semver = [int(g or 0) for g in m.groups()] if m else None
    if commit:
        key, label = "commit:" + commit[:7], commit[:7]
    else:
        key = "tag:" + "-".join(t for t in tokens if t not in _ENV_WORDS and not (built and t == str(built)))
        label = tag if len(tag) <= 22 else tag[:20] + "…"
    return {"name": name, "tag": tag, "key": key, "label": label, "commit": commit, "built": built, "semver": semver}


def workload_key(kind, namespace, name):
    """Same shape the UI uses for workload deep links: <namespace>/<Kind>/<name>."""
    return f"{namespace}/{kind}/{name}"


def _node_ready(node):
    conditions = node.get("conditions")
    if isinstance(conditions, dict):
        ready = conditions.get("Ready")
        return ready in (True, "True") if ready is not None else True
    if isinstance(conditions, list):
        for c in conditions:
            if isinstance(c, dict) and c.get("type") == "Ready":
                return c.get("status") == "True"
    return True


def summarize(state):
    """Compact island for the UI plus the workload index compare() needs. Never includes pod specs or env."""
    alerts = state.get("alerts") or []
    critical = sum(1 for a in alerts if a.get("severity") == "critical")
    workloads = {}
    seen_pod_workloads = False

    def entry(kind, ns, name):
        key = workload_key(kind, ns, name)
        w = workloads.get(key)
        if w is None:
            w = workloads[key] = {"kind": kind, "namespace": ns, "name": name, "desired": None, "ready": None,
                                  "specImages": [], "podImages": set(), "nodes": {}, "pods": 0}
        return w

    for n in state.get("nodes") or []:
        for p in n.get("pods") or []:
            status = str(p.get("status") or "")
            wl = p.get("workload")
            if not (isinstance(wl, dict) and wl.get("kind") and wl.get("name")):
                continue
            seen_pod_workloads = True
            kn = (p.get("labels") or {}).get("serving.knative.dev/service")
            w = entry("KnativeService", p.get("namespace") or "default", kn) if kn else entry(wl["kind"], p.get("namespace") or "default", wl["name"])
            w["pods"] += 1
            w["nodes"][n.get("name")] = w["nodes"].get(n.get("name"), 0) + 1
            if status == "Running":   # never leftover (Terminating) or not-yet-started pods
                for c in p.get("containers") or []:
                    if not c.get("init") and c.get("image"):
                        w["podImages"].add(c["image"])

    listed = state.get("workloads")
    for lw in listed if isinstance(listed, list) else []:
        if not lw.get("kind") or not lw.get("name"):
            continue
        ns = lw.get("namespace") or "default"
        if lw.get("knativeService"):
            # Knative keeps a Deployment per revision; the app is the Service, served by its non-idle revisions
            e = entry("KnativeService", ns, lw["knativeService"])
            if lw.get("desired"):
                e["desired"] = (e["desired"] or 0) + lw["desired"]
                e["ready"] = (e["ready"] or 0) + (lw.get("ready") or 0)
                e["specImages"] += lw.get("images") or []
            elif e["desired"] is None:
                e["desired"], e["ready"] = 0, 0
            continue
        e = entry(lw["kind"], ns, lw["name"])
        e["desired"] = lw.get("desired")
        e["ready"] = lw.get("ready")
        e["specImages"] = list(lw.get("images") or [])

    for w in workloads.values():
        # The spec says what it should run; fall back to running pods for states compiled before specs were kept
        pod_images = {i for i in w["podImages"] if not _MESH_SIDECAR.search(i)} or w["podImages"]   # a gateway *is* the proxy
        raw = sorted(set(w["specImages"])) or sorted(pod_images)
        w["images"] = sorted({normalize_image(i) for i in raw})
        w["paths"] = sorted({_strip_registry(i) for i in raw})
        w["imageSource"] = "spec" if w["specImages"] else "pods"
        w["nodes"] = sorted(w["nodes"], key=lambda k: -w["nodes"][k])
        w["running"] = w["pods"] > 0 or bool(w["desired"])
        del w["specImages"], w["podImages"]

    return {
        "clusterName": state.get("clusterName"),
        "generatedAt": state.get("generatedAt"),
        "nodes": _nodes(state),
        "alerts": {"critical": critical, "warning": len(alerts) - critical},
        "pending": len(state.get("unscheduledPods") or []),
        "workloads": workloads,
        # Older compiled states have neither a workloads list nor pod.workload: nothing to compare
        "hasWorkloadInfo": seen_pod_workloads or isinstance(listed, list) and bool(listed),
    }


def _nodes(state):
    """Buildings and rooms for drawing an island: [name, namespace, memoryGB, color, failing, status] per room."""
    out = []
    for n in state.get("nodes") or []:
        rooms = []
        for p in n.get("pods") or []:
            status = str(p.get("status") or "")
            rooms.append([p.get("name") or "", p.get("namespace") or "default", round(float(p.get("memoryGB") or 0), 3),
                          int(p.get("color") or 0x64748b), 1 if _FAILING.search(status) else 0, status])
        out.append({"name": n.get("name"), "mem": round(float(n.get("maxMemoryGB") or 0), 3),
                    "cpu": round(float(n.get("maxCPUCores") or 0), 2), "ready": _node_ready(n),
                    "cordoned": bool(n.get("unschedulable")), "pods": rooms})
    return out


# ---------- What gets compared ----------

def load_config(data):
    """archipelago.json: {"ignoreNamespaces": [glob], "ignoreWorkloads": ["ns/Kind/name" glob], "alwaysCompare": [glob],
    "pairs": [[ctxA, ctxB]], "fleetShare": 0.5}. Unknown keys are ignored; bad types raise ValueError."""
    cfg = {"ignoreNamespaces": [], "ignoreWorkloads": [], "alwaysCompare": [], "pairs": [], "fleetShare": FLEET_SHARE}
    if not data:
        return cfg
    if not isinstance(data, dict):
        raise ValueError("archipelago.json must be a JSON object")
    for k in ("ignoreNamespaces", "ignoreWorkloads", "alwaysCompare"):
        v = data.get(k, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(f"'{k}' must be a list of strings")
        cfg[k] = v
    pairs = data.get("pairs", [])
    if not isinstance(pairs, list) or not all(isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) for x in p) for p in pairs):
        raise ValueError("'pairs' must be a list of [contextA, contextB]")
    cfg["pairs"] = pairs
    share = data.get("fleetShare", FLEET_SHARE)
    if not isinstance(share, (int, float)) or not 0 < share <= 1:
        raise ValueError("'fleetShare' must be a number in (0, 1]")
    cfg["fleetShare"] = float(share)
    return cfg


def fleet_standards(summaries, share=FLEET_SHARE):
    """(Kind, name) pairs running in at least `share` of the known clusters (and at least 3 of them)."""
    summaries = [s for s in summaries if s and s.get("hasWorkloadInfo")]
    if len(summaries) < FLEET_MIN_CLUSTERS:
        return set()
    need = max(FLEET_MIN_CLUSTERS, math.ceil(len(summaries) * share))
    counts = {}
    for s in summaries:
        for kn in {(w["kind"], w["name"]) for w in s["workloads"].values() if w["running"]}:
            counts[kn] = counts.get(kn, 0) + 1
    return {kn for kn, c in counts.items() if c >= need}


def hidden_reason(key, w, fleet, cfg):
    """Why a workload is left out of the comparison, or None for an app that is compared."""
    if any(fnmatch.fnmatchcase(key, g) for g in cfg["alwaysCompare"]):
        return None
    if any(fnmatch.fnmatchcase(w["namespace"], g) for g in cfg["ignoreNamespaces"]) or \
       any(fnmatch.fnmatchcase(key, g) for g in cfg["ignoreWorkloads"]):
        return "config"
    if w["kind"] in ("Job", "CronJob"):
        return "batch"
    words = set(re.split(r"[-_.]+", w["name"].lower())) | set(re.split(r"[-_.]+", w["namespace"].lower()))
    if words & _INGRESS_WORDS or any(_INGRESS_IMAGE.search(p) for p in w["paths"]):
        return "ingress"
    if security.is_platform_workload(w["namespace"], {"kind": w["kind"]}) or w["namespace"].startswith(PLATFORM_NAMESPACE_PREFIXES):
        return "platform"
    if (w["kind"], w["name"]) in fleet:
        return "fleet"
    if _EPHEMERAL.search(w["name"]):
        return "ephemeral"
    if not w["running"]:
        return "idle"
    return None


def _versions(w):
    out = {}
    for img in w["images"]:
        v = version_of(img)
        out.setdefault(v["name"], []).append(v)
    return out


def _version_diff(h, o):
    """(differs, here, there): the first image name whose version differs, preferring the one named like the app."""
    hv, ov = _versions(h), _versions(o)
    shared = sorted(set(hv) & set(ov), key=lambda n: (n not in h["name"] and h["name"] not in n, n))
    if not shared:
        if not hv or not ov:
            return False, None, None
        a, b = next(iter(hv.values()))[0], next(iter(ov.values()))[0]
        return {x["key"] for vs in hv.values() for x in vs} != {x["key"] for vs in ov.values() for x in vs}, a, b
    for n in shared:
        if {x["key"] for x in hv[n]} != {x["key"] for x in ov[n]}:
            return True, hv[n][0], ov[n][0]
    return False, hv[shared[0]][0], ov[shared[0]][0]


def _newer(a, b):
    """'home' / 'other' / None, from semver or the build time in the tag; None when the tag can't tell."""
    if a["semver"] and b["semver"] and a["semver"] != b["semver"] and not (a["commit"] or b["commit"]):
        return "home" if a["semver"] > b["semver"] else "other"
    if a["built"] and b["built"] and a["built"] != b["built"]:
        return "home" if a["built"] > b["built"] else "other"
    return None


def _side(w, v):
    return {"version": v["label"] if v else None, "tag": v["tag"] if v else None, "image": v["name"] if v else None,
            "built": v["built"] if v else None, "nodes": w["nodes"], "pods": w["pods"],
            "desired": w["desired"], "ready": w["ready"], "imageSource": w["imageSource"]}


def compare(home, other, fleet=frozenset(), cfg=None):
    """Apps both clusters run, and whether they run the same version. Platform, fleet-standard, batch, idle and
    ephemeral workloads are counted under `hidden`; their version differences go to `fleetVersions` (Ops only)."""
    cfg = cfg or load_config(None)
    hw, ow = home["workloads"], other["workloads"]
    hidden = {}
    happs, oapps = {}, {}
    for side, ws, apps in (("home", hw, happs), ("other", ow, oapps)):
        for key, w in ws.items():
            reason = hidden_reason(key, w, fleet, cfg)
            if reason:
                hidden.setdefault(reason, set()).add(key)
            else:
                apps[key] = w

    # Match on namespace/Kind/name, then on Kind/name when unique on both sides (per-environment namespaces)
    pairs = [(k, k) for k in sorted(set(happs) & set(oapps))]
    taken_h, taken_o = {p[0] for p in pairs}, {p[1] for p in pairs}

    def by_kind_name(apps, taken):
        index = {}
        for key, w in apps.items():
            if key not in taken:
                index.setdefault((w["kind"], w["name"]), []).append(key)
        return {k: v[0] for k, v in index.items() if len(v) == 1}

    hk, ok = by_kind_name(happs, taken_h), by_kind_name(oapps, taken_o)
    for kn in sorted(set(hk) & set(ok)):
        pairs.append((hk[kn], ok[kn]))
        taken_h.add(hk[kn])
        taken_o.add(ok[kn])

    apps = []
    for hkey, okey in pairs:
        h, o = happs[hkey], oapps[okey]
        differs, hv, ov = _version_diff(h, o)
        newer = _newer(hv, ov) if differs and hv and ov else None
        apps.append({"key": hkey, "otherKey": okey, "kind": h["kind"], "namespace": h["namespace"], "name": h["name"],
                     "otherNamespace": o["namespace"] if o["namespace"] != h["namespace"] else None,
                     "status": "different" if differs else "same", "newer": newer,
                     "home": _side(h, hv), "other": _side(o, ov)})
    # Only on one side; "idleThere" when the other side has it scaled to zero rather than not at all
    idle_h = {(hw[k]["kind"], hw[k]["name"]) for k in hidden.get("idle", ()) if k in hw}
    idle_o = {(ow[k]["kind"], ow[k]["name"]) for k in hidden.get("idle", ()) if k in ow}
    for key in sorted(set(happs) - taken_h):
        apps.append({"key": key, "kind": happs[key]["kind"], "namespace": happs[key]["namespace"], "name": happs[key]["name"],
                     "status": "onlyHome", "idleThere": (happs[key]["kind"], happs[key]["name"]) in idle_o,
                     "home": _side(happs[key], None), "other": None})
    for key in sorted(set(oapps) - taken_o):
        apps.append({"key": key, "kind": oapps[key]["kind"], "namespace": oapps[key]["namespace"], "name": oapps[key]["name"],
                     "status": "onlyOther", "idleHere": (oapps[key]["kind"], oapps[key]["name"]) in idle_h,
                     "home": None, "other": _side(oapps[key], None)})
    order = {"different": 0, "onlyHome": 1, "onlyOther": 2, "same": 3}
    apps.sort(key=lambda a: (order[a["status"]], a["namespace"], a["name"]))

    # Shared tooling that differs: worth knowing (cert-manager 1.12 here, 1.11 there), but not app drift
    fleet_versions = []
    for key in sorted(set(hw) & set(ow)):
        if key in happs or key in oapps or not (hw[key]["running"] and ow[key]["running"]):
            continue
        differs, hv, ov = _version_diff(hw[key], ow[key])
        if differs:
            fleet_versions.append({"key": key, "kind": hw[key]["kind"], "namespace": hw[key]["namespace"], "name": hw[key]["name"],
                                   "here": hv["label"], "there": ov["label"]})

    counts = {s: sum(1 for a in apps if a["status"] == s) for s in order}
    return {
        "apps": apps,
        "counts": counts,
        "hidden": {r: len(keys) for r, keys in sorted(hidden.items())},
        "hiddenKeys": {r: sorted(keys)[:200] for r, keys in sorted(hidden.items())},
        "fleetVersions": fleet_versions,
    }


# ---------- Which clusters are compared ----------

def system_name(context):
    """A context's name without account/region prefixes and environment words: what its siblings share."""
    base = context.rsplit("/", 1)[-1].rsplit(":", 1)[-1].lower()
    words = [w for w in re.split(r"[-_.]+", base) if w and w not in _ENV_WORDS]
    return "-".join(words)


def are_siblings(a, b, cfg=None):
    """Two environments of the same system (or a pair named in archipelago.json)."""
    cfg = cfg or load_config(None)
    if {a, b} == {"demo", DEMO_ISLAND}:
        return True
    if any({a, b} == set(p) for p in cfg["pairs"]):
        return True
    sa = system_name(a)
    return bool(sa) and sa == system_name(b) and a != b


def suggest(active, contexts, warm=(), cfg=None):
    """Up to MAX_ISLANDS contexts to show next to `active`: its environment siblings, then warm contexts."""
    out = []
    if active == "demo":
        out.append(DEMO_ISLAND)
    else:
        out += [c for c in contexts if c not in (active, "demo") and are_siblings(active, c, cfg)]
    out += [c for c in warm if c not in out and c not in (active, "demo")]
    return out[:MAX_ISLANDS]


def demo_variant(state):
    """The demo cluster as a slightly different staging environment: one node fewer, an older checkout and
    redis, and fewer replicas of the database, so a version drift is visible offline."""
    s = copy.deepcopy(state)
    s["clusterName"] = DEMO_ISLAND
    nodes = s.get("nodes") or []
    if len(nodes) > 2:
        nodes.pop()
    swaps = {"checkout:1.42.0": "checkout:1.40.3", "checkout:1.41.0": "checkout:1.40.3", "redis:7-alpine": "redis:6.2-alpine"}

    def swap(img):
        for old, new in swaps.items():
            if img.endswith(old):
                return img[: -len(old)] + new
        return img
    for n in nodes:
        for p in n.get("pods") or []:
            for c in p.get("containers") or []:
                c["image"] = swap(c.get("image") or "")
    for w in s.get("workloads") or []:
        w["images"] = [swap(i) for i in w.get("images") or []]
        if w.get("kind") == "StatefulSet" and w.get("desired"):
            w["desired"] = max(1, w["desired"] - 1)
            w["ready"] = min(w.get("ready") or 0, w["desired"])
    s["alerts"] = [a for a in s.get("alerts") or [] if a.get("severity") != "critical"]
    return s
