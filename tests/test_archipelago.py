"""Tests for the F-36 archipelago (app version drift between environments). Run with: python3 -m unittest discover tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import archipelago as A  # noqa: E402


def pod(name, ns, kind, wl, images, status="Running", labels=None):
    return {"name": name, "namespace": ns, "memoryGB": 1, "color": 0x123456, "status": status, "labels": labels or {},
            "workload": {"kind": kind, "name": wl}, "containers": [{"name": f"c{i}", "image": img} for i, img in enumerate(images)]}


def state(nodes, workloads=None, alerts=None):
    s = {"clusterName": "c", "nodes": [{"name": n, "maxMemoryGB": 16, "maxCPUCores": 4, "pods": pods} for n, pods in nodes.items()]}
    if workloads is not None:
        s["workloads"] = [dict({"kind": k, "namespace": ns, "name": name, "desired": d, "ready": d}, **extra)
                          for (k, ns, name, d, *rest) in workloads for extra in [rest[0] if rest else {}]]
    s["alerts"] = alerts or []
    return s


def app_state(version, ns="shop", replicas=2, extra_pods=()):
    return state({"n1": [pod("api-1", ns, "Deployment", "api", [f"reg/api:{version}"])] + list(extra_pods)},
                 workloads=[("Deployment", ns, "api", replicas, {"images": [f"1111.dkr.ecr.us-east-1.amazonaws.com/team/api:{version}"]})])


class VersionTest(unittest.TestCase):
    def test_commit_is_the_version(self):
        dev, prod = A.version_of("r/app:build-dev-3f9a2c1-1787852932"), A.version_of("r/app:build-prod-3f9a2c1-1787900000")
        self.assertEqual(dev["key"], prod["key"])                 # same build promoted, env-specific tags
        self.assertEqual(dev["label"], "3f9a2c1")
        self.assertEqual(dev["built"], 1787852932)

    def test_all_digit_short_sha(self):
        self.assertEqual(A.version_of("r/app:build-dev-7654321-1775000000")["commit"], "7654321")

    def test_semver_and_plain_tags(self):
        self.assertEqual(A.version_of("r/cert-manager-controller:v1.12.8")["key"], "tag:v1-12-8")
        self.assertEqual(A.version_of("r/web:stable-prod")["key"], A.version_of("r/web:stable-dev")["key"])

    def test_registry_is_ignored(self):
        self.assertEqual(A.normalize_image("1111.dkr.ecr.us-east-1.amazonaws.com/team/api:1.4"), "api:1.4")
        self.assertEqual(A.normalize_image("localhost:5000/api"), "api:latest")


class ClassificationTest(unittest.TestCase):
    def reason(self, ns, kind, name, images=("app:1",), running=True, fleet=frozenset()):
        w = {"kind": kind, "namespace": ns, "name": name, "paths": list(images), "running": running}
        return A.hidden_reason(A.workload_key(kind, ns, name), w, fleet, A.load_config(None))

    def test_apps_are_compared(self):
        self.assertIsNone(self.reason("payments", "Deployment", "checkout"))

    def test_platform_and_node_agents(self):
        self.assertEqual(self.reason("datadog", "Deployment", "datadog-cluster-agent"), "platform")
        self.assertEqual(self.reason("flux-system", "Deployment", "source-controller"), "platform")
        self.assertEqual(self.reason("payments", "DaemonSet", "log-agent"), "platform")

    def test_ingress_and_gateways_by_name_or_image(self):
        self.assertEqual(self.reason("ingress-nginx", "Deployment", "ingress-nginx-controller"), "ingress")
        self.assertEqual(self.reason("dev", "Deployment", "istio-gateway"), "ingress")
        self.assertEqual(self.reason("edge", "Deployment", "front", images=("registry.k8s.io/ingress-nginx/controller:v1.9",)), "ingress")

    def test_fleet_standard_batch_idle_ephemeral(self):
        self.assertEqual(self.reason("tools", "Deployment", "common", fleet={("Deployment", "common")}), "fleet")
        self.assertEqual(self.reason("shop", "Job", "report-29011744"), "batch")
        self.assertEqual(self.reason("shop", "Deployment", "api", running=False), "idle")
        self.assertEqual(self.reason("previews", "Deployment", "pr-000977e91159405e9b513da6f9eeaa3b-770"), "ephemeral")

    def test_config_overrides(self):
        cfg = A.load_config({"ignoreNamespaces": ["sandbox-*"], "alwaysCompare": ["payments/Deployment/payment-gateway"]})
        w = {"kind": "Deployment", "namespace": "payments", "name": "payment-gateway", "paths": [], "running": True}
        self.assertIsNone(A.hidden_reason("payments/Deployment/payment-gateway", w, set(), cfg))
        w2 = dict(w, namespace="sandbox-1", name="x")
        self.assertEqual(A.hidden_reason("sandbox-1/Deployment/x", w2, set(), cfg), "config")
        with self.assertRaises(ValueError):
            A.load_config({"pairs": [["only-one"]]})

    def test_fleet_needs_three_clusters_and_half(self):
        s = lambda *names: A.summarize(state({"n": [pod(n, "x", "Deployment", n, ["a:1"]) for n in names]}))
        self.assertEqual(A.fleet_standards([s("dd"), s("dd")]), set())
        self.assertEqual(A.fleet_standards([s("dd", "a"), s("dd"), s("dd", "b"), s("c")]), {("Deployment", "dd")})


class SummarizeTest(unittest.TestCase):
    def test_spec_images_win_and_sidecars_are_dropped(self):
        st = state({"n1": [pod("api-1", "shop", "Deployment", "api", ["reg/api:2", "docker.io/istio/proxyv2:1.23.2"]),
                           pod("api-0", "shop", "Deployment", "api", ["reg/api:1"], status="Terminating")]})
        w = A.summarize(st)["workloads"]["shop/Deployment/api"]
        self.assertEqual(w["images"], ["api:2"])                    # no mesh sidecar, no leftover pod
        self.assertEqual(w["imageSource"], "pods")
        w = A.summarize(app_state("3"))["workloads"]["shop/Deployment/api"]
        self.assertEqual((w["images"], w["imageSource"]), (["api:3"], "spec"))

    def test_knative_revisions_group_into_their_service(self):
        st = state({"n": [pod("r2-pod", "fn", "Deployment", "hello-00002-deployment", ["hello:2"], labels={"serving.knative.dev/service": "hello"})]},
                   workloads=[("Deployment", "fn", "hello-00001-deployment", 0, {"images": ["hello:1"], "knativeService": "hello"}),
                              ("Deployment", "fn", "hello-00002-deployment", 1, {"images": ["hello:2"], "knativeService": "hello"})])
        ws = A.summarize(st)["workloads"]
        self.assertEqual(list(ws), ["fn/KnativeService/hello"])
        self.assertEqual((ws["fn/KnativeService/hello"]["images"], ws["fn/KnativeService/hello"]["desired"]), (["hello:2"], 1))

    def test_old_state_without_workload_info(self):
        self.assertFalse(A.summarize({"nodes": [{"name": "n", "pods": [{"name": "p", "namespace": "x"}]}]})["hasWorkloadInfo"])


class CompareTest(unittest.TestCase):
    def test_version_drift_and_newer_side(self):
        r = A.compare(A.summarize(app_state("build-dev-aaaaaaa-1790000000")), A.summarize(app_state("build-prod-bbbbbbb-1780000000")))
        a = r["apps"][0]
        self.assertEqual((a["status"], a["home"]["version"], a["other"]["version"], a["newer"]), ("different", "aaaaaaa", "bbbbbbb", "home"))
        self.assertEqual(a["home"]["nodes"], ["n1"])

    def test_replicas_are_not_drift(self):
        r = A.compare(A.summarize(app_state("1.0", replicas=2)), A.summarize(app_state("1.0", replicas=9)))
        self.assertEqual(r["counts"]["same"], 1)

    def test_platform_is_hidden_but_its_versions_are_listed(self):
        cm = lambda v: pod("cm", "cert-manager", "Deployment", "cert-manager", [f"cert-manager-controller:{v}"])
        r = A.compare(A.summarize(app_state("1", extra_pods=[cm("v1.12.8")])), A.summarize(app_state("1", extra_pods=[cm("v1.11.0")])))
        self.assertEqual(r["hidden"], {"platform": 1})
        self.assertEqual([(f["name"], f["here"], f["there"]) for f in r["fleetVersions"]], [("cert-manager", "v1.12.8", "v1.11.0")])
        self.assertEqual(r["counts"]["different"], 0)

    def test_env_namespaces_match_and_idle_is_flagged(self):
        home = A.summarize(state({"n": [pod("a", "dev", "Deployment", "api", ["api:1"])]},
                                 workloads=[("Deployment", "dev", "api", 1), ("Deployment", "dev", "batcher", 0)]))
        other = A.summarize(state({"n": [pod("a", "prod", "Deployment", "api", ["api:1"]), pod("b", "prod", "Deployment", "batcher", ["b:1"])]}))
        r = A.compare(home, other)
        by = {a["name"]: a for a in r["apps"]}
        self.assertEqual((by["api"]["status"], by["api"]["otherNamespace"]), ("same", "prod"))
        self.assertEqual((by["batcher"]["status"], by["batcher"]["idleHere"]), ("onlyOther", True))


class SiblingTest(unittest.TestCase):
    def test_env_siblings(self):
        a, b = "arn:aws:eks:us-east-1:1:cluster/shop-integ", "arn:aws:eks:us-east-1:2:cluster/shop-prod"
        self.assertTrue(A.are_siblings(a, b))
        self.assertFalse(A.are_siblings(a, "arn:aws:eks:us-east-1:2:cluster/prod-cluster"))
        self.assertTrue(A.are_siblings("dev-cluster", "prod-cluster"))
        self.assertTrue(A.are_siblings("x", "y", A.load_config({"pairs": [["y", "x"]]})))

    def test_suggest_siblings_then_warm(self):
        ctxs = ["c/shop-integ", "c/shop-prod", "c/prod-cluster", "c/shop-dev"]
        self.assertEqual(A.suggest(ctxs[0], ctxs, warm=["c/prod-cluster"]), ["c/shop-prod", "c/shop-dev", "c/prod-cluster"])
        self.assertEqual(A.suggest("demo", ["demo", "x"]), [A.DEMO_ISLAND])

    def test_demo_variant_drifts(self):
        base = state({"n1": [pod("c", "production", "Deployment", "checkout", ["reg/checkout:1.42.0"])], "n2": [], "n3": []},
                     workloads=[("Deployment", "production", "checkout", 2, {"images": ["reg/checkout:1.42.0"]})])
        r = A.compare(A.summarize(base), A.summarize(A.demo_variant(base)))
        self.assertEqual([(a["name"], a["status"]) for a in r["apps"]], [("checkout", "different")])


if __name__ == "__main__":
    unittest.main()
