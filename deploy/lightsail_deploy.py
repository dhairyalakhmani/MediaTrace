"""Host the MCP server on AWS Lightsail (ap-south-1), reachable over HTTPS with per-user tokens.

    python deploy/lightsail_deploy.py plan      # what it would do; checks permissions, changes nothing
    python deploy/lightsail_deploy.py deploy    # create/update the server (idempotent)
    python deploy/lightsail_deploy.py status

What `deploy` does:
  1. creates a small Ubuntu 24.04 Lightsail instance "lemkb-mcp" (1 GB RAM) with a static IP
  2. opens ports 443/80 to the internet (HTTPS + certificate challenge) and 22 to this machine only
  3. lets that static IP reach RDS (adds it to the database's security group, port 5432)
  4. uploads the code and a server .env (read-only DB login + S3 access) over SSH
  5. runs deploy/bootstrap.sh: Python app under systemd + Caddy with an automatic HTTPS certificate
     for <static-ip>.sslip.io (a public DNS name that maps to the IP; swap for a company domain later)
  6. checks https://<host>/health and prints how to connect Claude Code / Claude Desktop

Needs the permissions in deploy/iam-policy.json on the AWS user in .env.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

import boto3
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ENV = dotenv_values(ROOT / ".env")
REGION = ENV.get("AWS_S3_REGION") or "ap-south-1"
NAME, IP_NAME = "lemkb-mcp", "lemkb-mcp-ip"


def aws(service: str):
    return boto3.client(service, region_name=REGION, aws_access_key_id=ENV.get("AWS_ACCESS_KEY_ID") or None,
                        aws_secret_access_key=ENV.get("AWS_SECRET_ACCESS_KEY") or None)


def my_ip() -> str:
    return urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10).read().decode().strip()


def rds_security_group() -> str:
    host = ENV["RDS_HOST"]
    for db in aws("rds").describe_db_instances()["DBInstances"]:
        if db["Endpoint"]["Address"] == host:
            return db["VpcSecurityGroups"][0]["VpcSecurityGroupId"]
    raise SystemExit(f"RDS instance for {host} not found")


def pick_bundle(ls) -> str:
    # general-purpose, with a public IPv4 address (IPv6-only bundles can't take a static IPv4 / sslip.io name)
    bundles = [b for b in ls.get_bundles()["bundles"] if b["isActive"] and "LINUX_UNIX" in b["supportedPlatforms"]
               and b["ramSizeInGb"] >= 1 and "ipv6" not in b["bundleId"]
               and not b["bundleId"].startswith(("c_", "g_", "m_", "r_"))]
    return min(bundles, key=lambda b: b["price"])["bundleId"]


def plan() -> None:
    ls = aws("lightsail")
    checks = {"lightsail": lambda: pick_bundle(ls), "rds security group": rds_security_group,
              "this machine's IP": my_ip}
    ok = True
    for label, fn in checks.items():
        try:
            print(f"  OK   {label}: {fn()}")
        except Exception as e:
            ok = False
            print(f"  NO   {label}: {type(e).__name__}: {str(e)[:120]}")
    print("\nReady to deploy." if ok else "\nMissing permissions: attach deploy/iam-policy.json to the AWS user, then re-run.")


def _wait(ls, what: str, fn, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return
        time.sleep(5)
    raise SystemExit(f"timed out waiting for {what}")


def _bundle(tmp: Path) -> Path:
    """code.tar.gz + server.env (read-only DB login, S3). Users/tokens live in the database."""
    b = tmp / "bundle"
    b.mkdir()
    with tarfile.open(b / "code.tar.gz", "w:gz") as tar:
        for rel in ("src/lemkb", "deploy/requirements-mcp.txt"):
            tar.add(ROOT / rel, arcname=rel, filter=lambda ti: None if "__pycache__" in ti.name else ti)
    server_env = {
        "POSTGRES_HOST": ENV["RDS_HOST"], "POSTGRES_PORT": ENV.get("RDS_PORT", "5432"), "POSTGRES_DB": ENV.get("RDS_DB", "lemkb"),
        "POSTGRES_USER": ENV["MCP_DB_USER"], "POSTGRES_PASSWORD": ENV["MCP_DB_PASSWORD"], "POSTGRES_SSLMODE": "require",
        "S3_ENDPOINT": "", "S3_BUCKET": ENV["S3_BUCKET"], "S3_REGION": ENV["S3_REGION"],
        "AWS_ACCESS_KEY_ID": ENV["AWS_ACCESS_KEY_ID"], "AWS_SECRET_ACCESS_KEY": ENV["AWS_SECRET_ACCESS_KEY"],
        "CONTACT_EMAIL": ENV.get("CONTACT_EMAIL", ""),
    }
    (b / "server.env").write_text("".join(f"{k}={v}\n" for k, v in server_env.items()), encoding="utf-8")
    (b / "bootstrap.sh").write_bytes((ROOT / "deploy" / "bootstrap.sh").read_bytes().replace(b"\r\n", b"\n"))
    return b


def deploy() -> None:
    ls, ec2 = aws("lightsail"), aws("ec2")
    names = {i["name"] for i in ls.get_instances()["instances"]}
    if NAME not in names:
        bundle_id = pick_bundle(ls)
        print(f"creating instance {NAME} ({bundle_id}, ubuntu_24_04)")
        ls.create_instances(instanceNames=[NAME], availabilityZone=f"{REGION}a", blueprintId="ubuntu_24_04",
                            bundleId=bundle_id, tags=[{"key": "project", "value": "lemnisca-media-kb"}])
    _wait(ls, "instance running", lambda: ls.get_instance_state(instanceName=NAME)["state"]["name"] == "running")
    try:  # get_static_ip (not the plural list call) so the policy stays minimal
        ls.get_static_ip(staticIpName=IP_NAME)
    except ls.exceptions.NotFoundException:
        ls.allocate_static_ip(staticIpName=IP_NAME)
    ip = ls.get_static_ip(staticIpName=IP_NAME)["staticIp"]
    if not ip.get("isAttached"):
        ls.attach_static_ip(staticIpName=IP_NAME, instanceName=NAME)
    ip = ls.get_static_ip(staticIpName=IP_NAME)["staticIp"]["ipAddress"]
    host = ip.replace(".", "-") + ".sslip.io"
    admin = my_ip()
    ls.put_instance_public_ports(instanceName=NAME, portInfos=[
        {"fromPort": 443, "toPort": 443, "protocol": "tcp", "cidrs": ["0.0.0.0/0"]},
        {"fromPort": 80, "toPort": 80, "protocol": "tcp", "cidrs": ["0.0.0.0/0"]},
        {"fromPort": 22, "toPort": 22, "protocol": "tcp", "cidrs": [f"{admin}/32"]},
    ])
    sg = rds_security_group()
    try:
        ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
            "IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
            "IpRanges": [{"CidrIp": f"{ip}/32", "Description": "lemkb-mcp Lightsail server"}]}])
        print(f"RDS now accepts {ip}")
    except ec2.exceptions.ClientError as e:
        if "Duplicate" not in str(e):
            raise
    key = ls.download_default_key_pair()["privateKeyBase64"]
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        pem = tmp / "key.pem"
        pem.write_text(key, encoding="utf-8")
        if os.name != "nt":
            pem.chmod(0o600)
        else:  # OpenSSH on Windows refuses keys readable by others
            subprocess.run(["icacls", str(pem), "/inheritance:r", "/grant:r", f"{os.environ['USERNAME']}:R"],
                           check=True, capture_output=True)
        bundle = _bundle(tmp)
        ssh = ["ssh", "-i", str(pem), "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20", f"ubuntu@{ip}"]
        _wait(ls, "ssh", lambda: subprocess.run(ssh + ["true"], capture_output=True).returncode == 0, timeout=300)
        # a leftover bundle dir from a failed run would make scp nest the new one inside it
        subprocess.run(ssh + ["sudo rm -rf /tmp/lemkb-bundle"], check=True)
        subprocess.run(["scp", "-i", str(pem), "-o", "StrictHostKeyChecking=accept-new", "-r", str(bundle),
                        f"ubuntu@{ip}:/tmp/lemkb-bundle"], check=True)
        subprocess.run(ssh + [f"sudo PUBLIC_HOST={host} bash /tmp/lemkb-bundle/bootstrap.sh /tmp/lemkb-bundle "
                              "&& rm -rf /tmp/lemkb-bundle"], check=True)
    url = f"https://{host}"
    _wait(ls, "HTTPS health check", lambda: _healthy(url), timeout=300)
    print(f"\nHosted: {url}/mcp  (health: {url}/health)")
    print(f'Claude Code:    claude mcp add --scope user --transport http lemnisca-media-kb-remote {url}/mcp '
          f'--header "Authorization: Bearer <token>"')
    print("Claude Desktop: see README > Hosted MCP server")


def _healthy(url: str) -> bool:
    try:
        return urllib.request.urlopen(f"{url}/health", timeout=10).status == 200
    except Exception:
        return False


def status() -> None:
    ls = aws("lightsail")
    i = ls.get_instance(instanceName=NAME)["instance"]
    ip = i.get("publicIpAddress")
    url = f"https://{ip.replace('.', '-')}.sslip.io" if ip else None
    print(json.dumps({"state": i["state"]["name"], "ip": ip, "url": url, "healthy": _healthy(url) if url else False}, indent=2))


if __name__ == "__main__":
    {"plan": plan, "deploy": deploy, "status": status}.get((sys.argv[1:] or ["plan"])[0], plan)()
