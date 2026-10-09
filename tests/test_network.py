"""Tests for the F-21 network exposure engine. Run with: python3 -m unittest discover tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import network  # noqa: E402
import parse_cluster  # noqa: E402
import security  # noqa: E402

ALL_LISTED = {"Service": True, "Ingress": True, "NetworkPolicy": True}


def pod(name, ns="shop", labels=None, phase="Running", host_network=False):
    return {"metadata": {"name": name, "namespace": ns, "labels": labels or {}},
            "spec": {"nodeName": "node-a", "hostNetwork": host_network, "containers": [{"name": "app", "image": "app:1"}]},
            "status": {"phase": phase}}


def service(name, ns="shop", type_="ClusterIP", selector=None, annotations=None, lb=None, **spec):
    obj = {"kind": "Service", "metadata": {"name": name, "namespace": ns, "annotations": annotations or {}},
           "spec": {"type": type_, "selector": selector, "ports": [{"port": 443, "protocol": "TCP", "nodePort": 30443}], **spec},
           "status": {}}
    if lb is not None:
        obj["status"]["loadBalancer"] = {"ingress": [{"hostname": h} if not h[0].isdigit() else {"ip": h} for h in lb]}
    return obj


def ingress(name, services, ns="shop", hosts=("shop.example.com",), tls=True, annotations=None, klass="alb"):
    rules = [{"host": h, "http": {"paths": [{"path": "/", "backend": {"service": {"name": s, "port": {"number": 80}}}} for s in services]}}
             for h in hosts]
    spec = {"ingressClassName": klass, "rules": rules}
    if tls:
        spec["tls"] = [{"hosts": list(hosts)}]
    return {"kind": "Ingress", "metadata": {"name": name, "namespace": ns, "annotations": annotations or {}},
            "spec": spec, "status": {"loadBalancer": {"ingress": [{"hostname": "k8s-x.elb.amazonaws.com"}]}}}


def policy(name, ns="shop", selector=None, types=None, ingress_rules=None, egress_rules=None):
    spec = {"podSelector": selector if selector is not None else {}}
    if types is not None:
        spec["policyTypes"] = types
    if ingress_rules is not None:
        spec["ingress"] = ingress_rules
    if egress_rules is not None:
        spec["egress"] = egress_rules
    return {"kind": "NetworkPolicy", "metadata": {"name": name, "namespace": ns}, "spec": spec}


def analyze(items, pods, listed=ALL_LISTED):
    return network.analyze({"items": items, "lexNetwork": listed}, pods)


class SelectorTests(unittest.TestCase):
    def test_match_labels_and_expressions(self):
        sel = {"matchLabels": {"app": "web"},
               "matchExpressions": [{"key": "tier", "operator": "In", "values": ["front", "edge"]},
                                    {"key": "canary", "operator": "DoesNotExist"}]}
        self.assertTrue(network.selector_matches(sel, {"app": "web", "tier": "edge"}))
        self.assertFalse(network.selector_matches(sel, {"app": "web", "tier": "back"}))
        self.assertFalse(network.selector_matches(sel, {"app": "web", "tier": "edge", "canary": "1"}))
        self.assertTrue(network.selector_matches({"matchExpressions": [{"key": "env", "operator": "NotIn", "values": ["prod"]}]}, {}))
        self.assertTrue(network.selector_matches({"matchExpressions": [{"key": "env", "operator": "Exists"}]}, {"env": "x"}))

    def test_empty_matches_all_and_none_matches_nothing(self):
        self.assertTrue(network.selector_matches({}, {"anything": "x"}))
        self.assertFalse(network.selector_matches(None, {"anything": "x"}))
        self.assertEqual(network.selector_text({}), "all pods")


class GateTests(unittest.TestCase):
    def test_public_load_balancer_without_policy_is_high(self):
        summary, per_pod = analyze([service("web", type_="LoadBalancer", selector={"app": "web"}, lb=["abc.elb.amazonaws.com"])],
                                   [pod("web-1", labels={"app": "web"}), pod("other", labels={"app": "x"})])
        gate = summary["gates"][0]
        self.assertEqual((gate["kind"], gate["scope"], gate["risk"]), ("LoadBalancer", "public", "high"))
        self.assertEqual(gate["pods"], ["shop/web-1"])
        self.assertEqual(per_pod["shop/web-1"]["finding"][0], "public-no-netpol")
        self.assertEqual(per_pod["shop/other"]["finding"], ("no-netpol", ""))

    def test_internal_load_balancer_by_annotation_and_address(self):
        by_annotation = network._service_gates(service("a", type_="LoadBalancer", selector={"app": "a"},
                                                        annotations={"service.beta.kubernetes.io/aws-load-balancer-internal": "true"}))[0]
        by_hostname = network._service_gates(service("b", type_="LoadBalancer", selector={"app": "b"},
                                                      lb=["internal-k8s-b-123.us-east-1.elb.amazonaws.com"]))[0]
        by_ip = network._service_gates(service("c", type_="LoadBalancer", selector={"app": "c"}, lb=["10.0.3.4"]))[0]
        self.assertEqual([g["scope"] for g in (by_annotation, by_hostname, by_ip)], ["internal"] * 3)
        self.assertTrue(by_annotation["pending"])

    def test_nodeport_and_source_ranges_are_medium(self):
        summary, per_pod = analyze([service("np", type_="NodePort", selector={"app": "np"}),
                                    service("lb", type_="LoadBalancer", selector={"app": "lb"}, lb=["x.elb"],
                                            loadBalancerSourceRanges=["203.0.113.0/24"])],
                                   [pod("np-1", labels={"app": "np"}), pod("lb-1", labels={"app": "lb"})])
        risks = {g["name"]: g["risk"] for g in summary["gates"]}
        self.assertEqual(risks, {"np": "medium", "lb": "medium"})
        self.assertEqual(per_pod["shop/np-1"]["finding"][0], "exposed-no-netpol")
        self.assertEqual(per_pod["shop/lb-1"]["finding"][0], "exposed-no-netpol")

    def test_cluster_ip_services_are_not_gates(self):
        summary, _ = analyze([service("internal", selector={"app": "x"})], [pod("x", labels={"app": "x"})])
        self.assertEqual(summary["gates"], [])

    def test_ingress_reaches_pods_through_its_services(self):
        summary, per_pod = analyze([service("api", selector={"app": "api"}), ingress("shop", ["api", "ghost"])],
                                   [pod("api-1", labels={"app": "api"})])
        gate = summary["gates"][0]
        self.assertEqual((gate["kind"], gate["tls"], gate["podCount"]), ("Ingress", "all", 1))
        self.assertEqual(gate["missingServices"], ["ghost"])
        self.assertIn("ing:shop/shop", per_pod["shop/api-1"]["gates"])

    def test_ingress_with_v1beta1_backend_and_partial_tls(self):
        ing = ingress("old", [], hosts=("a.example.com", "b.example.com"), tls=False)
        ing["spec"]["tls"] = [{"hosts": ["a.example.com"]}]
        ing["spec"]["backend"] = {"serviceName": "legacy", "servicePort": 80}
        gate = network._ingress_gate(ing)
        self.assertEqual(gate["services"], ["legacy"])
        self.assertEqual(gate["tls"], "partial")

    def test_internal_ingress_class(self):
        gate = network._ingress_gate(ingress("i", ["s"], klass="nginx-internal"))
        self.assertEqual(gate["scope"], "internal")

    def test_terminated_pods_are_ignored(self):
        summary, per_pod = analyze([service("web", type_="LoadBalancer", selector={"app": "web"}, lb=["x"])],
                                   [pod("done", labels={"app": "web"}, phase="Succeeded")])
        self.assertEqual(summary["gates"][0]["risk"], "idle")
        self.assertNotIn("shop/done", per_pod)


class PolicyTests(unittest.TestCase):
    def test_policy_isolates_selected_pods(self):
        summary, per_pod = analyze([service("web", type_="LoadBalancer", selector={"app": "web"}, lb=["x"]),
                                    policy("web-in", selector={"matchLabels": {"app": "web"}},
                                           ingress_rules=[{"from": [{"podSelector": {"matchLabels": {"app": "lb"}}}]}])],
                                   [pod("web-1", labels={"app": "web"})])
        self.assertEqual(summary["gates"][0]["risk"], "protected")
        self.assertTrue(per_pod["shop/web-1"]["isolated"])
        self.assertNotIn("finding", per_pod["shop/web-1"])
        self.assertEqual(summary["totals"]["protected"], 1)

    def test_allow_all_rule_does_not_protect(self):
        for rules in ([{}], [{"from": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}]):
            summary, per_pod = analyze([service("web", type_="LoadBalancer", selector={"app": "web"}, lb=["x"]),
                                        policy("open", selector={}, ingress_rules=rules)],
                                       [pod("web-1", labels={"app": "web"})])
            self.assertEqual(summary["gates"][0]["risk"], "high")
            self.assertTrue(per_pod["shop/web-1"]["open"])
            self.assertIn("allow ingress from anywhere", per_pod["shop/web-1"]["finding"][1])
            self.assertTrue(summary["policies"][0]["allowAllIngress"])

    def test_egress_only_policy_does_not_isolate_ingress(self):
        _, per_pod = analyze([policy("egress", types=["Egress"], egress_rules=[{"to": [{"podSelector": {}}]}])],
                             [pod("a")])
        self.assertFalse(per_pod["shop/a"]["isolated"])
        self.assertTrue(per_pod["shop/a"]["egress"])
        self.assertEqual(per_pod["shop/a"]["finding"][0], "no-netpol")

    def test_default_deny_namespace(self):
        summary, per_pod = analyze([policy("deny", selector={}, types=["Ingress"])], [pod("a"), pod("b", ns="other")])
        ns = {n["namespace"]: n for n in summary["namespaces"]}
        self.assertTrue(ns["shop"]["defaultDeny"])
        self.assertEqual(ns["shop"]["protected"], 1)
        self.assertEqual(ns["other"]["protected"], 0)
        self.assertTrue(summary["policies"][0]["denyAllIngress"])

    def test_policies_only_apply_in_their_namespace(self):
        _, per_pod = analyze([policy("deny", ns="other", selector={})], [pod("a")])
        self.assertEqual(per_pod["shop/a"]["finding"][0], "no-netpol")

    def test_host_network_pods_get_no_netpol_finding(self):
        _, per_pod = analyze([], [pod("agent", host_network=True)])
        self.assertNotIn("shop/agent", per_pod)


class ListingTests(unittest.TestCase):
    def test_nothing_listed_means_no_summary(self):
        self.assertEqual(network.analyze({"items": []}, [pod("a")]), (None, {}))
        self.assertEqual(network.analyze(None, [pod("a")]), (None, {}))

    def test_policies_not_listed_means_unknown_not_unprotected(self):
        summary, per_pod = analyze([service("web", type_="LoadBalancer", selector={"app": "web"}, lb=["x"])],
                                   [pod("web-1", labels={"app": "web"})],
                                   listed={"Service": True, "Ingress": True})
        self.assertEqual(summary["gates"][0]["risk"], "unknown")
        self.assertIsNone(summary["gates"][0]["unprotectedPods"])
        self.assertNotIn("finding", per_pod["shop/web-1"])
        self.assertIsNone(summary["totals"]["protected"])


class IntegrationTests(unittest.TestCase):
    def test_findings_reach_the_security_summary(self):
        p = pod("web-1", labels={"app": "web"})
        _, per_pod = analyze([service("web", type_="LoadBalancer", selector={"app": "web"}, lb=["x"])], [p])
        sec = security.summarize(p, "shop", None, per_pod["shop/web-1"]["finding"])
        self.assertEqual(sec["risk"], "high")
        self.assertIn("public-no-netpol", [f["code"] for f in sec["findings"]])

    def test_compile_state_carries_network(self):
        p = pod("web-1", labels={"app": "web"})
        nodes = {"items": [{"metadata": {"name": "node-a"}, "status": {"allocatable": {"memory": "8Gi", "cpu": "2"}}}]}
        wl = {"items": [service("web", type_="LoadBalancer", selector={"app": "web"}, lb=["x"])], "lexNetwork": ALL_LISTED}
        state = parse_cluster.compile_state(nodes, {"items": [p]}, wl, "test", None, verbose=False)
        self.assertEqual(state["network"]["gates"][0]["id"], "svc:shop/web")
        compiled = state["nodes"][0]["pods"][0]
        self.assertEqual(compiled["network"], {"gates": ["svc:shop/web"]})
        self.assertEqual(compiled["security"]["risk"], "high")

    def test_compile_state_without_network_kinds(self):
        nodes = {"items": [{"metadata": {"name": "node-a"}, "status": {"allocatable": {"memory": "8Gi", "cpu": "2"}}}]}
        state = parse_cluster.compile_state(nodes, {"items": [pod("a")]}, {"items": []}, "test", None, verbose=False)
        self.assertIsNone(state["network"])
        self.assertNotIn("network", state["nodes"][0]["pods"][0])


if __name__ == "__main__":
    unittest.main()
