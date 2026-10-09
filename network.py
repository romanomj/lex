#!/usr/bin/env python3
"""
Network exposure (F-21): which pods can be reached from outside the cluster, and which pods no
NetworkPolicy protects.

Inputs are the raw Service, Ingress and NetworkPolicy objects (fetched alongside workloads) and the raw pods.
Outputs:
  - "gates": every way into the cluster from outside: LoadBalancer and NodePort Services, Services with
    externalIPs, and Ingresses, each with the pods behind it and how many of those no policy restricts;
  - NetworkPolicy coverage per namespace, and a short summary of each policy;
  - per pod: whether a policy isolates it for ingress, whether its rules still allow traffic from anywhere,
    which gates lead to it, and the posture finding that goes with that (see security.FINDING_INFO).

The engine is pure (no kubectl), so fixtures can test it. It reads policies the way the API defines them; it
can't tell whether the cluster's CNI actually enforces them, and it doesn't model where load-balanced traffic
appears to come from (externalTrafficPolicy), so "allowed" is about the rules, not the packets.
"""

import ipaddress

NETWORK_KINDS = ("Service", "Ingress", "NetworkPolicy")
MAX_GATE_PODS = 100                 # pod keys listed per gate (the count is always exact)

# Annotations that make a LoadBalancer internal (AWS, GCP, Azure, OCI, IBM...)
INTERNAL_LB_ANNOTATIONS = {
    "service.beta.kubernetes.io/aws-load-balancer-internal": None,             # any truthy value
    "service.beta.kubernetes.io/aws-load-balancer-scheme": "internal",
    "networking.gke.io/load-balancer-type": "internal",
    "cloud.google.com/load-balancer-type": "internal",
    "service.beta.kubernetes.io/azure-load-balancer-internal": "true",
    "service.beta.kubernetes.io/oci-load-balancer-internal": "true",
    "service.kubernetes.io/oci-load-balancer-internal": "true",
    "service.kubernetes.io/ibm-load-balancer-cloud-provider-ip-type": "private",
}
INTERNAL_INGRESS_ANNOTATIONS = {
    "alb.ingress.kubernetes.io/scheme": "internal",
    "kubernetes.io/ingress.class": None,     # checked by name below (e.g. "nginx-internal")
}
INTERNAL_CLASS_HINTS = ("internal", "private")
OPEN_CIDRS = ("0.0.0.0/0", "::/0")


def _md(obj):
    return obj.get("metadata") or {}


def _key(namespace, name):
    return f"{namespace}/{name}"


# ---------- label selectors ----------

def selector_matches(selector, labels):
    """A metav1.LabelSelector against a label map. An empty selector matches everything; None matches nothing."""
    if selector is None:
        return False
    labels = labels or {}
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr.get("key"), expr.get("operator"), expr.get("values") or []
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and key in labels and labels[key] in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
        if op not in ("In", "NotIn", "Exists", "DoesNotExist"):
            return False    # unknown operator: the API server would have rejected it; match nothing to be safe
    return True


def selector_text(selector):
    """Short human form: 'app=web, tier in (a,b)', or 'all pods' for an empty selector."""
    if not selector or (not selector.get("matchLabels") and not selector.get("matchExpressions")):
        return "all pods"
    parts = [f"{k}={v}" for k, v in sorted((selector.get("matchLabels") or {}).items())]
    for e in selector.get("matchExpressions") or []:
        op = e.get("operator")
        if op in ("Exists", "DoesNotExist"):
            parts.append(("!" if op == "DoesNotExist" else "") + str(e.get("key")))
        else:
            parts.append(f"{e.get('key')} {'notin' if op == 'NotIn' else 'in'} ({','.join(map(str, e.get('values') or []))})")
    return ", ".join(parts)


# ---------- NetworkPolicies ----------

def _policy_types(spec):
    types = spec.get("policyTypes")
    if types:
        return [t for t in types if t in ("Ingress", "Egress")]
    # Defaults per the API: Ingress always; Egress only when egress rules are present
    return ["Ingress"] + (["Egress"] if spec.get("egress") else [])


def _peer_is_anywhere(peer):
    block = peer.get("ipBlock") or {}
    return block.get("cidr") in OPEN_CIDRS and not block.get("except")


def _rule_allows_anywhere(rule):
    """An ingress rule with no `from` (or an ipBlock of 0.0.0.0/0) admits traffic from any source."""
    peers = rule.get("from")
    return not peers or any(_peer_is_anywhere(p) for p in peers)


def summarize_policy(item):
    md, spec = _md(item), item.get("spec") or {}
    types = _policy_types(spec)
    ingress = spec.get("ingress") or []
    egress = spec.get("egress") or []
    selector = spec.get("podSelector") if spec.get("podSelector") is not None else {}
    return {
        "namespace": md.get("namespace", "default"),
        "name": md.get("name"),
        "selector": selector,
        "selectorText": selector_text(selector),
        "types": types,
        "ingressRules": len(ingress),
        "egressRules": len(egress),
        "denyAllIngress": "Ingress" in types and not ingress,
        "denyAllEgress": "Egress" in types and not egress,
        "allowAllIngress": "Ingress" in types and any(_rule_allows_anywhere(r) for r in ingress),
    }


# ---------- Services and Ingresses ----------

def _is_private_address(addr):
    try:
        return ipaddress.ip_address(addr).is_private
    except ValueError:
        return False


def _lb_addresses(status):
    out = []
    for entry in ((status.get("loadBalancer") or {}).get("ingress") or []):
        addr = entry.get("hostname") or entry.get("ip")
        if addr:
            out.append(addr)
    return out


def _annotation_says_internal(annotations, table):
    for key, wanted in table.items():
        value = annotations.get(key)
        if value is None:
            continue
        value = str(value).strip().lower()
        if wanted is None:
            if key == "kubernetes.io/ingress.class":
                if any(h in value for h in INTERNAL_CLASS_HINTS):
                    return True
            elif value not in ("", "false", "0"):
                return True
        elif value == wanted:
            return True
    return False


def _addresses_internal(addresses):
    """All published addresses are private IPs, or AWS internal ELB names (internal-...elb.amazonaws.com)."""
    if not addresses:
        return False
    return all(_is_private_address(a) or a.startswith("internal-") for a in addresses)


def _service_ports(spec, with_node_ports):
    ports = []
    for p in spec.get("ports") or []:
        label = f"{p.get('port')}/{p.get('protocol') or 'TCP'}"
        if with_node_ports and p.get("nodePort"):
            label += f" (node {p.get('nodePort')})"
        ports.append(label)
    return ports


def _service_gates(item):
    """The gates one Service opens (LoadBalancer, NodePort, externalIPs); usually zero or one."""
    md, spec, status = _md(item), item.get("spec") or {}, item.get("status") or {}
    stype = spec.get("type") or "ClusterIP"
    annotations = md.get("annotations") or {}
    ns, name = md.get("namespace", "default"), md.get("name")
    base = {"namespace": ns, "name": name, "services": [name], "selector": spec.get("selector") or None}
    gates = []
    if stype == "LoadBalancer":
        addresses = _lb_addresses(status)
        ranges = [r for r in spec.get("loadBalancerSourceRanges") or []]
        internal = _annotation_says_internal(annotations, INTERNAL_LB_ANNOTATIONS) or _addresses_internal(addresses)
        gates.append({**base, "id": f"svc:{ns}/{name}", "kind": "LoadBalancer",
                      "scope": "internal" if internal else "public",
                      "addresses": addresses[:4], "pending": not addresses,
                      "ports": _service_ports(spec, False),
                      "sourceRanges": ranges[:6],
                      "restricted": bool(ranges) and not any(r in OPEN_CIDRS for r in ranges)})
    elif stype == "NodePort":
        gates.append({**base, "id": f"svc:{ns}/{name}", "kind": "NodePort", "scope": "node",
                      "addresses": [], "pending": False, "ports": _service_ports(spec, True),
                      "sourceRanges": [], "restricted": False})
    external_ips = spec.get("externalIPs") or []
    if external_ips and stype != "ExternalName":
        gates.append({**base, "id": f"eip:{ns}/{name}", "kind": "ExternalIP",
                      "scope": "internal" if all(_is_private_address(a) for a in external_ips) else "public",
                      "addresses": external_ips[:4], "pending": False, "ports": _service_ports(spec, False),
                      "sourceRanges": [], "restricted": False})
    return gates


def _backend_service(backend):
    """networking.k8s.io/v1 ({service: {name}}) or the older v1beta1 form ({serviceName})."""
    if not backend:
        return None
    return (backend.get("service") or {}).get("name") or backend.get("serviceName")


def _ingress_gate(item):
    md, spec, status = _md(item), item.get("spec") or {}, item.get("status") or {}
    annotations = md.get("annotations") or {}
    ns, name = md.get("namespace", "default"), md.get("name")
    klass = spec.get("ingressClassName") or annotations.get("kubernetes.io/ingress.class")
    services, hosts, paths = [], [], 0
    default = _backend_service(spec.get("defaultBackend") or spec.get("backend"))
    if default:
        services.append(default)
    for rule in spec.get("rules") or []:
        if rule.get("host"):
            hosts.append(rule["host"])
        for p in ((rule.get("http") or {}).get("paths") or []):
            paths += 1
            svc = _backend_service(p.get("backend"))
            if svc and svc not in services:
                services.append(svc)
    tls_hosts = {h for t in spec.get("tls") or [] for h in (t.get("hosts") or [])}
    if not spec.get("tls"):
        tls = "none"
    elif not hosts or all(h in tls_hosts for h in hosts):
        tls = "all"
    else:
        tls = "partial"
    addresses = _lb_addresses(status)
    internal = (_annotation_says_internal(annotations, INTERNAL_INGRESS_ANNOTATIONS)
                or any(h in str(klass or "").lower() for h in INTERNAL_CLASS_HINTS)
                or _addresses_internal(addresses))
    ranges = [r.strip() for r in str(annotations.get("alb.ingress.kubernetes.io/inbound-cidrs")
                                     or annotations.get("nginx.ingress.kubernetes.io/whitelist-source-range") or "").split(",") if r.strip()]
    return {"id": f"ing:{ns}/{name}", "kind": "Ingress", "namespace": ns, "name": name,
            "scope": "internal" if internal else "public",
            "addresses": addresses[:4], "pending": not addresses,
            "class": klass, "hosts": hosts[:8], "hostCount": len(hosts), "paths": paths, "tls": tls,
            "ports": ["443/TCP" if tls != "none" else "80/TCP"],
            "services": services, "selector": None,
            "sourceRanges": ranges[:6], "restricted": bool(ranges) and not any(r in OPEN_CIDRS for r in ranges)}


# ---------- analysis ----------

def network_items(workloads_data):
    """(items by kind, which kinds were listed). Older dumps and fixtures without the marker have none."""
    data = workloads_data or {}
    listed = data.get("lexNetwork") or {}
    by_kind = {k: [] for k in NETWORK_KINDS}
    for item in data.get("items") or []:
        if item.get("kind") in by_kind:
            by_kind[item["kind"]].append(item)
    return by_kind, {k: bool(listed.get(k)) for k in NETWORK_KINDS}


def _gate_risk(gate, policies_known):
    if not gate["podCount"]:
        return "idle"
    if not policies_known:
        return "unknown"
    if not gate["unprotectedPods"]:
        return "protected"
    return "high" if gate["scope"] == "public" and not gate["restricted"] else "medium"


def gate_label(gate):
    return f"{gate['kind']} {gate['namespace']}/{gate['name']}"


def analyze(workloads_data, pods):
    """
    Returns (summary, per_pod). summary is None when no network kind was listed (RBAC, older dumps); per_pod maps
    "namespace/name" -> {"isolated", "open", "egress", "policies", "gates", "finding"} for live (non-terminated) pods.
    """
    by_kind, listed = network_items(workloads_data)
    if not any(listed.values()):
        return None, {}
    policies_known = listed["NetworkPolicy"]

    live = []
    for pod in pods or []:
        md, spec = _md(pod), pod.get("spec") or {}
        if (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
            continue
        live.append((md.get("namespace", "default"), md.get("name"), md.get("labels") or {}, bool(spec.get("hostNetwork"))))
    pods_by_ns = {}
    for entry in live:
        pods_by_ns.setdefault(entry[0], []).append(entry)

    # Policies: which pods each selects, and the union of what they allow
    policies = []
    per_pod = {}
    for item in by_kind["NetworkPolicy"]:
        pol = summarize_policy(item)
        selected = 0
        for ns, name, labels, host_net in pods_by_ns.get(pol["namespace"], []):
            if not selector_matches(pol["selector"], labels):
                continue
            selected += 1
            info = per_pod.setdefault(_key(ns, name), {"isolated": False, "open": False, "egress": False, "policies": []})
            info["policies"].append(pol["name"])
            if "Ingress" in pol["types"]:
                info["isolated"] = True
                info["open"] = info["open"] or pol["allowAllIngress"]
            if "Egress" in pol["types"]:
                info["egress"] = True
        pol["pods"] = selected
        del pol["selector"]
        policies.append(pol)
    policies.sort(key=lambda p: (p["namespace"], p["name"]))

    def protected(ns, name):
        info = per_pod.get(_key(ns, name))
        return bool(info and info["isolated"] and not info["open"])

    # Gates and the pods behind them
    services = {}
    gates = []
    for item in by_kind["Service"]:
        md, spec = _md(item), item.get("spec") or {}
        services[_key(md.get("namespace", "default"), md.get("name"))] = spec.get("selector") or None
        gates.extend(_service_gates(item))
    for item in by_kind["Ingress"]:
        gates.append(_ingress_gate(item))

    for gate in gates:
        ns = gate["namespace"]
        if gate["kind"] == "Ingress":
            selectors, missing = [], []
            for svc in gate["services"]:
                key = _key(ns, svc)
                if key in services:
                    if services[key]:
                        selectors.append(services[key])
                elif listed["Service"]:
                    missing.append(svc)
            gate["missingServices"] = missing
        else:
            selectors = [gate["selector"]] if gate["selector"] else []
        del gate["selector"]
        backing = [(n, name, host_net) for n, name, labels, host_net in pods_by_ns.get(ns, [])
                   if any(selector_matches({"matchLabels": s}, labels) for s in selectors)]
        gate["podCount"] = len(backing)
        gate["pods"] = [_key(n, name) for n, name, _ in backing[:MAX_GATE_PODS]]
        gate["unprotectedPods"] = sum(1 for n, name, _ in backing if not protected(n, name)) if policies_known else None
        gate["risk"] = _gate_risk(gate, policies_known)
        for n, name, host_net in backing:
            info = per_pod.setdefault(_key(n, name), {"isolated": False, "open": False, "egress": False, "policies": []})
            info.setdefault("gates", []).append(gate["id"])
            info.setdefault("_gates", []).append(gate)

    risk_order = {"high": 0, "medium": 1, "unknown": 2, "protected": 3, "idle": 4}
    gates.sort(key=lambda g: (risk_order[g["risk"]], g["scope"] != "public", g["namespace"], g["name"], g["kind"]))

    # Findings: the strongest of exposed-without-policy (public or not) and not-covered-at-all
    if policies_known:
        for ns, name, labels, host_net in live:
            key = _key(ns, name)
            if host_net:
                continue    # NetworkPolicy doesn't apply to host-network pods (they have their own finding)
            info = per_pod.get(key)
            if info and info["isolated"] and not info["open"]:
                continue
            exposed = (info or {}).get("_gates") or []
            public = [g for g in exposed if g["scope"] == "public" and not g["restricted"]]
            why = "its policies allow ingress from anywhere" if info and info["open"] else None
            if public:
                finding = ("public-no-netpol", gate_label(public[0]) + (f" ({why})" if why else ""))
            elif exposed:
                finding = ("exposed-no-netpol", gate_label(exposed[0]) + (f" ({why})" if why else ""))
            else:
                finding = ("no-netpol", why or "")
            per_pod.setdefault(key, {"isolated": False, "open": False, "egress": False, "policies": []})["finding"] = finding
    for info in per_pod.values():
        info.pop("_gates", None)

    # Coverage per namespace (live pods that a policy isolates for ingress without opening it back up)
    namespaces = []
    for ns, entries in sorted(pods_by_ns.items()):
        ns_policies = [p for p in policies if p["namespace"] == ns]
        eligible = [e for e in entries if not e[3]]
        namespaces.append({
            "namespace": ns,
            "pods": len(eligible),
            "protected": sum(1 for e in eligible if protected(ns, e[1])) if policies_known else None,
            "exposed": sum(1 for e in entries if (per_pod.get(_key(ns, e[1])) or {}).get("gates")),
            "policies": len(ns_policies),
            "defaultDeny": any(p["selectorText"] == "all pods" and p["denyAllIngress"] for p in ns_policies),
        })

    exposed_keys = {k for k, v in per_pod.items() if v.get("gates")}
    eligible_all = [e for e in live if not e[3]]
    summary = {
        "listed": {"services": listed["Service"], "ingresses": listed["Ingress"], "networkPolicies": policies_known},
        "gates": gates,
        "policies": policies,
        "namespaces": namespaces,
        "totals": {
            "pods": len(eligible_all),
            "protected": sum(1 for e in eligible_all if protected(e[0], e[1])) if policies_known else None,
            "exposedPods": len(exposed_keys),
            "exposedUnprotected": sum(1 for k in exposed_keys if per_pod[k].get("finding", ("",))[0] in ("public-no-netpol", "exposed-no-netpol"))
                                  if policies_known else None,
            "publicGates": sum(1 for g in gates if g["scope"] == "public"),
            "services": len(by_kind["Service"]),
        },
    }
    return summary, per_pod


def pod_view(info):
    """The compact per-pod summary shipped in the state (omitted for pods with nothing to say)."""
    if not info:
        return None
    out = {}
    if info.get("isolated"):
        out["isolated"] = True
    if info.get("open"):
        out["open"] = True
    if info.get("egress"):
        out["egress"] = True
    if info.get("policies"):
        out["policies"] = info["policies"][:6]
    if info.get("gates"):
        out["gates"] = info["gates"][:6]
    return out or None
