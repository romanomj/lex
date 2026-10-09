#!/usr/bin/env python3
"""
Keeps secret values out of Lex's files and screens.

Pod specs often carry credentials as literal env values (or inside the
kubectl.kubernetes.io/last-applied-configuration annotation). Lex doesn't need those values, so:

- sanitize_list_for_disk(): before a raw kubectl dump is written to data/, every literal env value is
  replaced (names are kept, so the security scan can still flag secret-looking variables), and
  managedFields / last-applied-configuration are dropped.
- sanitize_pod_for_display() / redact_describe_text(): for the live spec and describe views, only
  values that look secret (by variable name, or credentials embedded in a URL) are hidden, so
  ordinary configuration stays readable while debugging.
"""

import os
import re
import json

SANITIZED_MARKER = "lex.dev/sanitized"
REDACTED = "[redacted by Lex]"
REDACTED_CREDENTIAL_URL = "[redacted by Lex: credential URL]"
# Hints kept in place of values that are clearly not secrets, so detection still works after redaction
REDACTED_TRIVIAL = "[redacted by Lex: flag or number]"
REDACTED_PATH = "[redacted by Lex: file path]"
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"

SECRET_NAME_RE = re.compile(r"(PASS(WORD|WD)?|SECRET|TOKEN|API[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL|ACCESS[_-]?KEY|AUTH)", re.I)
CREDENTIAL_URL_RE = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
# Names that mention secrets but hold something else (a path, an id, a toggle, a duration...)
BENIGN_NAME_RE = re.compile(r"(_FILE|_PATH|_DIR|_TTL|_EXPIR[A-Z]*|_ENABLED|_TIMEOUT|_HEADER|_NAME|_TYPE|_MODE|_LENGTH|_ENDPOINT|"
                            r"_URL|_URI|_HOST|_PORT|_ID|_REGION|_ISSUER|_AUDIENCE|_SCOPES?|_METHOD|_PROVIDER|_KIND|_FORMAT)$|"
                            r"^(ALLOW|ENABLE|DISABLE|USE|SKIP|REQUIRE)_", re.I)
TRIVIAL_VALUE_RE = re.compile(r"^(true|false|yes|no|on|off|none|null|enabled|disabled|required|optional|[0-9]{1,8})$", re.I)


def is_secret_like_name(name):
    return bool(name) and bool(SECRET_NAME_RE.search(name)) and not BENIGN_NAME_RE.search(name)


def _is_trivial_value(value):
    v = str(value or "")
    return v in (REDACTED_TRIVIAL, REDACTED_PATH) or bool(TRIVIAL_VALUE_RE.match(v)) or (v.startswith("/") and " " not in v)


def looks_like_secret(name, value):
    """A literal env value that is probably a credential: credentials in a URL, or a secret-like name
    holding something other than a flag, number or file path."""
    if value == REDACTED_CREDENTIAL_URL or has_credential_url(value):
        return True
    return is_secret_like_name(name) and bool(value) and not _is_trivial_value(value)


def has_credential_url(value):
    return bool(value) and bool(CREDENTIAL_URL_RE.search(str(value)))


def _containers(pod_spec):
    for key in ("initContainers", "containers", "ephemeralContainers"):
        for c in pod_spec.get(key) or []:
            yield c


def _clean_metadata(metadata):
    if not isinstance(metadata, dict):
        return
    metadata.pop("managedFields", None)
    annotations = metadata.get("annotations")
    if isinstance(annotations, dict):
        annotations.pop(LAST_APPLIED, None)


def _sanitize_pod_spec(pod_spec, redact_all):
    """Redacts literal env values in place. Returns the number of values hidden."""
    hidden = 0
    for c in _containers(pod_spec or {}):
        for env in c.get("env") or []:
            value = env.get("value")
            if not value or str(value).startswith("[redacted by Lex"):
                continue
            if has_credential_url(value):
                env["value"] = REDACTED_CREDENTIAL_URL
                hidden += 1
            elif looks_like_secret(env.get("name"), value):
                env["value"] = REDACTED
                hidden += 1
            elif redact_all:
                # Lex never needs config values on disk; keep only a hint for clearly non-secret ones
                env["value"] = REDACTED_TRIVIAL if TRIVIAL_VALUE_RE.match(str(value)) else (
                    REDACTED_PATH if str(value).startswith("/") and " " not in str(value) else REDACTED)
    return hidden


def _sanitize_object(obj, redact_all):
    """A pod, or a workload with a pod template. Mutates in place; returns values hidden."""
    _clean_metadata(obj.get("metadata"))
    spec = obj.get("spec") or {}
    hidden = 0
    if "containers" in spec:
        hidden += _sanitize_pod_spec(spec, redact_all)
    template = spec.get("template")
    if isinstance(template, dict):
        _clean_metadata(template.get("metadata"))
        hidden += _sanitize_pod_spec(template.get("spec") or {}, redact_all)
    return hidden


def sanitize_list_for_disk(data):
    """Sanitizes a kubectl `-o json` List before it is written to disk (all literal env values hidden)."""
    for item in data.get("items") or []:
        _sanitize_object(item, redact_all=True)
    data.setdefault("metadata", {})
    if isinstance(data["metadata"], dict):
        data["metadata"][SANITIZED_MARKER] = True
    return data


def sanitize_json_text_for_disk(text):
    data = json.loads(text)
    return json.dumps(sanitize_list_for_disk(data), separators=(",", ":"))


def is_sanitized(data):
    return isinstance(data.get("metadata"), dict) and data["metadata"].get(SANITIZED_MARKER) is True


def sanitize_pod_for_display(pod):
    """Hides only secret-looking values (by name or embedded credentials). Returns values hidden."""
    return _sanitize_object(pod, redact_all=False)


_DESCRIBE_LINE_RE = re.compile(r"^(\s+)([A-Za-z_][A-Za-z0-9_.-]*):(\s+)(\S.*)$")


def redact_describe_text(text):
    """Redacts secret-looking `NAME: value` lines (e.g. under Environment:) in `kubectl describe` output."""
    hidden = 0
    out = []
    for line in text.split("\n"):
        m = _DESCRIBE_LINE_RE.match(line)
        if m and looks_like_secret(m.group(2), m.group(4)) and not m.group(4).startswith("<set to the key") and not m.group(4).startswith("["):
            line = f"{m.group(1)}{m.group(2)}:{m.group(3)}{REDACTED}"
            hidden += 1
        elif has_credential_url(line):
            line = CREDENTIAL_URL_RE.sub("://[redacted by Lex]@", line)
            hidden += 1
        out.append(line)
    return "\n".join(out), hidden


def scrub_data_dir(data_dir, write_file_atomic):
    """One-time cleanup of dumps written before redaction existed; also tightens permissions."""
    scrubbed = 0
    if not os.path.isdir(data_dir):
        return scrubbed
    os.chmod(data_dir, 0o700)
    for name in os.listdir(data_dir):
        path = os.path.join(data_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if not (name.startswith("raw-") and name.endswith(".json")):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if is_sanitized(data):
                continue
            write_file_atomic(path, json.dumps(sanitize_list_for_disk(data), separators=(",", ":")))
            scrubbed += 1
        except (OSError, ValueError) as e:
            print(f"▲ Could not scrub {path}: {e}")
    return scrubbed
