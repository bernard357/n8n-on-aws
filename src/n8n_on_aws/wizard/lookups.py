"""Read-only AWS lookups that turn free-text questions into lists.

They run the AWS CLI, like ``cleanup`` and ``make prices``, so the package needs
no AWS SDK. The runner is injectable for tests. Offline, without the CLI or
without credentials, every lookup returns an empty list and the assistant asks
for the value as text instead.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=30
    )


class Zone(BaseModel):
    id: str
    name: str


class Certificate(BaseModel):
    arn: str
    domain: str


class Subnet(BaseModel):
    id: str
    zone: str
    public: bool
    name: str = ""


class Lookups:
    def __init__(self, offline: bool = False, runner: Runner | None = None) -> None:
        self.runner = runner or _run
        self.online = not offline and (runner is not None or shutil.which("aws"))

    def _json(self, *arguments: str) -> Any:
        if not self.online:
            return None
        try:
            result = self.runner(["aws", *arguments, "--output", "json"])
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout or "null")
        except json.JSONDecodeError:
            return None

    def default_region(self) -> str | None:
        if not self.online:
            return None
        try:
            result = self.runner(["aws", "configure", "get", "region"])
        except (OSError, subprocess.SubprocessError):
            return None
        region = result.stdout.strip() if result.returncode == 0 else ""
        return region or None

    def zones(self) -> list[Zone]:
        data = self._json("route53", "list-hosted-zones")
        zones = []
        for zone in (data or {}).get("HostedZones", []):
            if zone.get("Config", {}).get("PrivateZone"):
                continue
            zones.append(
                Zone(id=zone["Id"].split("/")[-1], name=zone["Name"].rstrip("."))
            )
        return sorted(zones, key=lambda z: z.name)

    def certificates(self, region: str) -> list[Certificate]:
        data = self._json(
            "acm",
            "list-certificates",
            "--region",
            region,
            "--certificate-statuses",
            "ISSUED",
        )
        return [
            Certificate(arn=c["CertificateArn"], domain=c["DomainName"])
            for c in (data or {}).get("CertificateSummaryList", [])
        ]

    def vpcs(self, region: str) -> list[tuple[str, str]]:
        data = self._json("ec2", "describe-vpcs", "--region", region)
        found = []
        for vpc in (data or {}).get("Vpcs", []):
            tags = {t["Key"]: t["Value"] for t in vpc.get("Tags", [])}
            label = f"{vpc['VpcId']} {tags.get('Name', '')} {vpc['CidrBlock']}"
            found.append((vpc["VpcId"], " ".join(label.split())))
        return found

    def subnets(self, region: str, vpc_id: str) -> list[Subnet]:
        data = self._json(
            "ec2",
            "describe-subnets",
            "--region",
            region,
            "--filters",
            f"Name=vpc-id,Values={vpc_id}",
        )
        found = []
        for subnet in (data or {}).get("Subnets", []):
            tags = {t["Key"]: t["Value"] for t in subnet.get("Tags", [])}
            found.append(
                Subnet(
                    id=subnet["SubnetId"],
                    zone=subnet["AvailabilityZone"],
                    public=bool(subnet.get("MapPublicIpOnLaunch")),
                    name=tags.get("Name", ""),
                )
            )
        return sorted(found, key=lambda s: (s.public, s.zone))

    def databases(self, region: str) -> list[tuple[str, str]]:
        """(endpoint, label) of PostgreSQL clusters and instances."""
        found = []
        clusters = self._json("rds", "describe-db-clusters", "--region", region)
        for cluster in (clusters or {}).get("DBClusters", []):
            if "postgres" in cluster.get("Engine", ""):
                found.append(
                    (
                        cluster["Endpoint"],
                        f"{cluster['DBClusterIdentifier']} ({cluster['Engine']})",
                    )
                )
        instances = self._json("rds", "describe-db-instances", "--region", region)
        for db in (instances or {}).get("DBInstances", []):
            if db.get("Engine") == "postgres" and "Endpoint" in db:
                found.append(
                    (db["Endpoint"]["Address"], f"{db['DBInstanceIdentifier']} (rds)")
                )
        return found

    def caches(self, region: str) -> list[tuple[str, str]]:
        """(endpoint, label) of ElastiCache Serverless caches and replication groups."""
        found = []
        serverless = self._json(
            "elasticache", "describe-serverless-caches", "--region", region
        )
        for cache in (serverless or {}).get("ServerlessCaches", []):
            endpoint = cache.get("Endpoint", {}).get("Address")
            if endpoint:
                found.append((endpoint, f"{cache['ServerlessCacheName']} (serverless)"))
        groups = self._json(
            "elasticache", "describe-replication-groups", "--region", region
        )
        for group in (groups or {}).get("ReplicationGroups", []):
            nodes = group.get("NodeGroups") or [{}]
            endpoint = nodes[0].get("PrimaryEndpoint", {}).get("Address")
            if endpoint:
                found.append((endpoint, f"{group['ReplicationGroupId']} (nodes)"))
        return found

    def secrets(self, region: str) -> list[str]:
        data = self._json("secretsmanager", "list-secrets", "--region", region)
        return sorted(s["Name"] for s in (data or {}).get("SecretList", []))

    def eks_clusters(self, region: str) -> list[str]:
        data = self._json("eks", "list-clusters", "--region", region)
        return sorted((data or {}).get("clusters", []))
