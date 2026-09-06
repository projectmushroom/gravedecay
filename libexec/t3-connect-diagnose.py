#!/usr/bin/env python3
"""Read-only Connect diagnostics. Never print bearer tokens or raw trace records."""
import argparse
import datetime
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

RELAY = "https://relay.t3.codes"
ERROR_CODES = (
    "environment_link_limit_exceeded", "environment_connect_not_authorized",
    "auth_invalid", "invalid_bearer", "environment_link_proof_expired",
    "environment_link_proof_invalid", "upstream_unavailable",
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Keep the account bearer on the intended origin, even on redirects.
        return None


def latest_link_attempt(base):
    logs = base / "userdata" / "logs"
    files = sorted(logs.glob("server.trace.ndjson*"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:2]
    latest = None
    for path in files:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - 4 * 1024 * 1024)
            stream.seek(offset)
            if offset:
                stream.readline()
            for line in stream:
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict) or not isinstance(record.get("exit"), dict):
                    continue
                if record.get("name") != "environment.cloud.reconcileDesiredLinkWith":
                    continue
                stamp = str(record.get("startTimeUnixNano", "0"))
                if not stamp.isdigit():
                    continue
                if latest is None or int(stamp) > latest[0]:
                    latest = (int(stamp), record.get("exit", {}))
    if latest is None:
        return {"result": "No recent link attempt in the trace log"}
    timestamp, outcome = latest
    summary = {"time": datetime.datetime.fromtimestamp(
        timestamp / 1e9, datetime.timezone.utc).isoformat()}
    if outcome.get("_tag") == "Success":
        summary["result"] = "Link reconciliation succeeded"
        return summary
    # Only allowlisted fields leave the trace: it may contain sensitive data.
    cause = json.dumps(outcome)
    summary["result"] = "Link reconciliation failed"
    match = re.search(r"\b([45][0-9]{2}) POST https://relay\.t3\.codes/v1/client/environment-links\b", cause)
    if match:
        summary["http_status"] = int(match.group(1))
    for code in ERROR_CODES:
        if code in cause:
            summary["code"] = code
            break
    if "PermissionDenied" in cause or "EACCES" in cause:
        summary["code"] = "local_permission_denied"
    return summary


def list_environments(base, opener=None):
    token_path = base / "userdata" / "secrets" / "cloud-cli-oauth-token.bin"
    token = json.loads(token_path.read_text())["accessToken"]
    if not isinstance(token, str) or not token:
        raise ValueError("missing access token")
    request = urllib.request.Request(RELAY + "/v1/environments",
                                     headers={"Authorization": "Bearer " + token,
                                              "User-Agent": "gravedecay-connect-diagnostics/1",
                                              "Accept": "application/json"})
    opener = opener or urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=20) as response:
        body = json.load(response)
    if not isinstance(body, dict) or not isinstance(body.get("environments"), list):
        raise ValueError("invalid environment response")
    result = []
    for row in body["environments"]:
        if not isinstance(row, dict) or not isinstance(row.get("endpoint"), dict):
            raise ValueError("invalid environment entry")
        if not all(isinstance(row.get(key), str) for key in ("environmentId", "label")):
            raise ValueError("invalid environment identity")
        # JSON rendering below escapes terminal control sequences in labels.
        result.append({"id": row["environmentId"], "label": row["label"],
                       "provider": row.get("endpoint", {}).get("providerKind", "unknown")})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_dir", type=pathlib.Path)
    parser.add_argument("--offline", action="store_true", help="inspect local traces only")
    args = parser.parse_args(argv)
    try:
        attempt = latest_link_attempt(args.base_dir)
    except OSError:
        print("Cannot read T3 traces as the service user; check file ownership.", file=sys.stderr)
        return 1
    print("Latest recorded link attempt: " + json.dumps(attempt))
    if attempt.get("code") == "environment_link_limit_exceeded":
        print("The relay reports a managed-tunnel limit. Deregister an unused environment, then restart t3code.")
    elif attempt.get("http_status") == 403:
        print("This T3 version did not preserve a recognized relay error body. HTTP 403 alone does not prove a tunnel limit.")
    if args.offline:
        return 0
    try:
        environments = list_environments(args.base_dir)
    except urllib.error.HTTPError as error:
        print("Environment list request failed: HTTP {}. Check authorization or retry later.".format(error.code), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print("Cannot list environments. Check service-user credential access and relay connectivity; no login was changed.", file=sys.stderr)
        return 1
    print("Registered environments:")
    for row in environments:
        print("  " + json.dumps(row, ensure_ascii=True))
    managed = sum(row["provider"] == "cloudflare_tunnel" for row in environments)
    print("Managed Cloudflare tunnels: {} (the list does not report your account limit).".format(managed))
    if attempt.get("result") != "Link reconciliation succeeded":
        print("If freeing a slot, deregister only an unused environment in your account's T3 Connect page, then restart t3code.")
    print("See docs/SECURITY.md: Freeing a managed-tunnel slot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
