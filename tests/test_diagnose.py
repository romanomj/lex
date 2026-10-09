"""Tests for the F-24 pod diagnosis engine. Run with: python3 -m unittest discover tests"""

import os
import sys
import datetime
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diagnose  # noqa: E402

NOW = datetime.datetime(2026, 10, 3, 12, 0, 0, tzinfo=datetime.timezone.utc)


def ts(minutes_ago=0, seconds_ago=0):
    return (NOW - datetime.timedelta(minutes=minutes_ago, seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_pod(container=None, cstatus=None, phase="Running", node="node-a", age_minutes=60, **extra):
    container = {"name": "app", "image": "registry.example.com/app:1.2.3",
                 "resources": {"requests": {"memory": "512Mi", "cpu": "250m"}, "limits": {"memory": "512Mi"}},
                 "readinessProbe": {"httpGet": {"path": "/ready", "port": 8080}}, **(container or {})}
    status = {"phase": phase}
    if cstatus is not None:
        status["containerStatuses"] = [{"name": container["name"], **cstatus}]
    pod = {
        "metadata": {"name": "app-7d4f9b8c-x2v4q", "namespace": "shop", "uid": "uid-1",
                     "creationTimestamp": ts(age_minutes),
                     "ownerReferences": [{"kind": "ReplicaSet", "name": "app-7d4f9b8c", "controller": True}],
                     "labels": {"pod-template-hash": "7d4f9b8c"}},
        "spec": {"nodeName": node, "containers": [container]},
        "status": status,
    }
    for key, value in extra.items():
        section, field = key.split("__")
        pod[section][field] = value
    return pod


def event(reason, message, type_="Warning", container="app", minutes_ago=1, count=1, uid="uid-1"):
    return {"type": type_, "reason": reason, "message": message, "count": count, "lastTimestamp": ts(minutes_ago),
            "involvedObject": {"kind": "Pod", "uid": uid, "fieldPath": f"spec.containers{{{container}}}" if container else None}}


def crashloop(last):
    return {"restartCount": 7, "ready": False,
            "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 2m40s restarting failed container"}},
            "lastState": {"terminated": last}}


def run(pod, events=None, **kw):
    return diagnose.diagnose(pod, {"items": events or []}, now=NOW, **kw)


class CrashTests(unittest.TestCase):
    def test_oomkilled_crashloop_names_the_limit(self):
        r = run(make_pod(cstatus=crashloop({"reason": "OOMKilled", "exitCode": 137, "startedAt": ts(3), "finishedAt": ts(2)})),
                usage={"peakMemoryBytes": 510 * 1024 ** 2})
        self.assertEqual(r["severity"], "critical")
        self.assertIn("out of memory", r["headline"])
        self.assertIn("512 MiB", r["summary"])
        self.assertTrue(any("Recent peak usage" in e for e in r["findings"][0]["evidence"]))
        self.assertIn({"action": "logs", "label": "Logs from the crashed run of 'app' (--previous)", "container": "app", "previous": True}, r["actions"])

    def test_liveness_probe_kill(self):
        pod = make_pod({"livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "timeoutSeconds": 1}},
                       crashloop({"reason": "Error", "exitCode": 137, "startedAt": ts(5), "finishedAt": ts(2)}))
        r = run(pod, [event("Unhealthy", "Liveness probe failed: Get \"http://10.0.0.1:8080/healthz\": context deadline exceeded"),
                      event("Killing", "Container app failed liveness probe, will be restarted", type_="Normal")])
        self.assertIn("liveness probe is failing", r["headline"])
        fixes = " ".join(r["findings"][0]["fixes"])
        self.assertIn("timeoutSeconds", fixes)
        self.assertIn("startupProbe", fixes)

    def test_quick_application_crash_quotes_termination_message(self):
        r = run(make_pod(cstatus=crashloop({"reason": "Error", "exitCode": 1, "startedAt": ts(seconds_ago=63), "finishedAt": ts(1),
                                            "message": "missing required env DATABASE_URL"})))
        self.assertIn("exit code 1", r["headline"])
        self.assertIn("startup problem", r["summary"])
        self.assertIn("missing required env DATABASE_URL", r["summary"])

    def test_exit_zero_loop(self):
        r = run(make_pod(cstatus=crashloop({"reason": "Completed", "exitCode": 0, "startedAt": ts(2), "finishedAt": ts(2)})))
        self.assertIn("exits immediately", r["headline"])

    def test_exec_format_error(self):
        r = run(make_pod(cstatus={"restartCount": 2, "state": {"waiting": {"reason": "RunContainerError",
                 "message": "exec /app/server: exec format error"}}}))
        self.assertIn("wrong CPU architecture", r["headline"])

    def test_recovered_oom_is_a_warning_with_previous_logs(self):
        r = run(make_pod(cstatus={"restartCount": 3, "ready": True, "state": {"running": {}},
                                  "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": ts(10)}}}))
        self.assertEqual(r["severity"], "warning")
        self.assertIn("OOMKilled 10m ago", r["headline"])
        self.assertTrue(any(a.get("previous") for a in r["actions"]))


class StartupTests(unittest.TestCase):
    def test_image_not_found(self):
        r = run(make_pod(cstatus={"state": {"waiting": {"reason": "ImagePullBackOff",
                 "message": "Back-off pulling image"}}}),
                [event("Failed", "Failed to pull image: rpc error: manifest unknown: manifest unknown")])
        self.assertIn("doesn't exist", r["headline"])

    def test_image_unauthorized(self):
        r = run(make_pod(cstatus={"state": {"waiting": {"reason": "ErrImagePull",
                 "message": "failed to authorize: 401 Unauthorized"}}}))
        self.assertIn("Not allowed to pull", r["headline"])

    def test_missing_secret(self):
        r = run(make_pod(cstatus={"state": {"waiting": {"reason": "CreateContainerConfigError",
                 "message": 'secret "db-credentials" not found'}}}))
        self.assertEqual(r["headline"], "Secret 'db-credentials' is missing")

    def test_out_of_ips(self):
        r = run(make_pod(cstatus={"state": {"waiting": {"reason": "ContainerCreating"}}}, age_minutes=8),
                [event("FailedCreatePodSandBox", "plugin type=\"aws-cni\" failed (add): add cmd: failed to assign an IP address to container", container=None)])
        self.assertIn("out of IP addresses", r["headline"])

    def test_young_container_creating_is_transient(self):
        r = run(make_pod(cstatus={"state": {"waiting": {"reason": "ContainerCreating"}}}, age_minutes=1))
        self.assertEqual(r["severity"], "info")
        self.assertEqual(r["headline"], "Still starting")

    def test_readiness_failing(self):
        r = run(make_pod(cstatus={"ready": False, "state": {"running": {"startedAt": ts(20)}}}),
                [event("Unhealthy", "Readiness probe failed: HTTP probe failed with statuscode: 503", count=40)])
        self.assertIn("running but not ready", r["headline"])
        self.assertTrue(any("(×40)" in e for e in r["findings"][0]["evidence"]))


class SchedulingTests(unittest.TestCase):
    def pending(self, message, age=30):
        return make_pod(phase="Pending", node=None, age_minutes=age,
                        status__conditions=[{"type": "PodScheduled", "status": "False", "reason": "Unschedulable",
                                             "message": message, "lastTransitionTime": ts(age)}])

    def test_insufficient_memory_with_grammar(self):
        r = run(self.pending("0/4 nodes are available: 1 node(s) were unschedulable, 3 Insufficient memory. "
                             "preemption: 0/4 nodes are available: 4 No preemption victims found for incoming pod."))
        self.assertIn("none of the 4 nodes fit", r["headline"])
        self.assertIn("1 node is cordoned", r["summary"])
        self.assertIn("3 nodes don't have enough free memory for its request of 512 MiB", r["summary"])

    def test_autoscaler_adding_a_node_is_transient(self):
        r = run(self.pending("0/3 nodes are available: 3 Insufficient cpu.", age=1),
                [event("TriggeredScaleUp", "pod triggered scale-up: [{eks-general 3->4 (max: 10)}]", type_="Normal", container=None)])
        self.assertEqual(r["severity"], "info")
        self.assertEqual(r["headline"], "Waiting for a new node")


class PodLevelTests(unittest.TestCase):
    def test_evicted(self):
        pod = make_pod(phase="Failed", status__reason="Evicted",
                       status__message="The node was low on resource: ephemeral-storage. Threshold quantity: 4Gi, available: 3Gi.")
        r = run(pod)
        self.assertIn("node low on disk space", r["headline"])
        self.assertIn("replacement", r["summary"])

    def test_stuck_terminating_with_finalizers(self):
        pod = make_pod(cstatus={"ready": True, "state": {"running": {}}},
                       metadata__deletionTimestamp=ts(15), metadata__deletionGracePeriodSeconds=30,
                       metadata__finalizers=["example.com/cleanup"])
        r = run(pod)
        self.assertIn("Stuck terminating", r["headline"])
        self.assertIn("example.com/cleanup", r["summary"])

    def test_node_not_ready_comes_first(self):
        r = run(make_pod(cstatus={"ready": True, "state": {"running": {}}}),
                node={"conditions": {"Ready": "Unknown", "ReadyReason": "NodeStatusUnknown"}})
        self.assertEqual(r["severity"], "critical")
        self.assertIn("is not ready", r["headline"])

    def test_node_pressure_ranks_after_the_pods_own_problem(self):
        pod = make_pod({"resources": {"requests": {"memory": "1Gi"}}},
                       {"restartCount": 3, "ready": True, "state": {"running": {}},
                        "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": ts(5)}}})
        r = run(pod, node={"conditions": {"Ready": "True", "MemoryPressure": "True"}})
        self.assertIn("OOMKilled", r["headline"])
        self.assertIn("memory pressure right now", r["summary"])
        self.assertEqual(r["findings"][1]["title"], "Its node is low on memory")

    def test_healthy_pod(self):
        r = run(make_pod(cstatus={"ready": True, "state": {"running": {"startedAt": ts(50)}}}))
        self.assertEqual(r["severity"], "ok")
        self.assertEqual(r["headline"], "No problems found")
        self.assertEqual(r["containers"][0]["probes"]["readiness"], "HTTP GET :8080/ready, every 10s, timeout 1s, fails after 3 misses")

    def test_events_from_an_older_pod_with_the_same_name_are_ignored(self):
        r = run(make_pod(cstatus={"ready": True, "state": {"running": {}}}),
                [event("BackOff", "old pod crashed", uid="uid-old")])
        self.assertEqual(r["events"], [])


if __name__ == "__main__":
    unittest.main()
