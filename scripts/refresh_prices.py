"""Refresh ``src/n8n_on_aws/finops/prices.json`` from the AWS Price List API.

A maintainer tool, not used at run time: the estimator reads the JSON file, so
``plan --cost`` makes no AWS call. It needs the AWS CLI and credentials (the Price
List API is free and read-only).

    python scripts/refresh_prices.py            # every region in REGIONS
    python scripts/refresh_prices.py eu-west-3  # just one, merged into the file
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
from pathlib import Path

OUTPUT = Path(__file__).parents[1] / "src" / "n8n_on_aws" / "finops" / "prices.json"

# Region -> the code AWS puts in front of most usage types.
REGIONS = {
    "us-east-1": "USE1",
    "us-east-2": "USE2",
    "us-west-2": "USW2",
    "ca-central-1": "CAN1",
    "eu-west-1": "EU",
    "eu-west-2": "EUW2",
    "eu-west-3": "EUW3",
    "eu-central-1": "EUC1",
    "eu-north-1": "EUN1",
    "ap-south-1": "APS3",
    "ap-southeast-1": "APS1",
    "ap-southeast-2": "APS2",
    "ap-northeast-1": "APN1",
}

AURORA_CLASSES = [
    "db.t4g.medium",
    "db.t4g.large",
    "db.r6g.large",
    "db.r7g.large",
    "db.r7g.xlarge",
    "db.r7g.2xlarge",
]
RDS_CLASSES = [
    "db.t4g.micro",
    "db.t4g.small",
    "db.t4g.medium",
    "db.t4g.large",
    "db.m7g.large",
    "db.r7g.large",
]
CACHE_NODES = [
    "cache.t4g.micro",
    "cache.t4g.small",
    "cache.t4g.medium",
    "cache.m7g.large",
    "cache.r7g.large",
    "cache.r7g.xlarge",
]

EC2_LINUX = {
    "operatingSystem": "Linux",
    "tenancy": "Shared",
    "preInstalledSw": "NA",
    "capacitystatus": "Used",
}

# key: (service, usage type without region code, description must contain, filters)
Spec = tuple[str, str, str, dict[str, str]]


def specs() -> dict[str, Spec]:
    found: dict[str, Spec] = {
        "fargate_vcpu_hour": ("AmazonECS", "Fargate-vCPU-Hours:perCPU", "", {}),
        "fargate_gb_hour": ("AmazonECS", "Fargate-GB-Hours", "", {}),
        "fargate_arm_vcpu_hour": (
            "AmazonECS",
            "Fargate-ARM-vCPU-Hours:perCPU",
            "",
            {},
        ),
        "fargate_arm_gb_hour": ("AmazonECS", "Fargate-ARM-GB-Hours", "", {}),
        "alb_hour": ("AWSELB", "LoadBalancerUsage", "Application LoadBalancer", {}),
        "alb_lcu_hour": ("AWSELB", "LCUUsage", "Application load balancer", {}),
        "nat_gateway_hour": ("AmazonEC2", "NatGateway-Hours", "", {}),
        "nat_gateway_gb": ("AmazonEC2", "NatGateway-Bytes", "", {}),
        "ec2_t4g_nano_hour": ("AmazonEC2", "BoxUsage:t4g.nano", "", EC2_LINUX),
        "public_ipv4_hour": ("AmazonVPC", "PublicIPv4:InUseAddress", "", {}),
        "vpc_endpoint_hour": ("AmazonVPC", "VpcEndpoint-Hours", "", {}),
        "aurora_acu_hour": (
            "AmazonRDS",
            "Aurora:ServerlessV2Usage",
            "PostgreSQL",
            {},
        ),
        "aurora_storage_gb_month": (
            "AmazonRDS",
            "Aurora:StorageUsage",
            "PostgreSQL",
            {},
        ),
        "aurora_io": ("AmazonRDS", "Aurora:StorageIOUsage", "PostgreSQL", {}),
        "rds_gp3_gb_month": ("AmazonRDS", "RDS:GP3-Storage", "running PostgreSQL", {}),
        "rds_gp3_multi_az_gb_month": (
            "AmazonRDS",
            "RDS:Multi-AZ-GP3-Storage",
            "running PostgreSQL",
            {},
        ),
        "valkey_serverless_gb_hour": ("AmazonElastiCache", "CachedData:Valkey", "", {}),
        "valkey_serverless_ecpu": (
            "AmazonElastiCache",
            "ElastiCacheProcessingUnits:Valkey",
            "",
            {},
        ),
        "efs_standard_gb_month": ("AmazonEFS", "TimedStorage-ByteHrs", "", {}),
        "s3_standard_gb_month": ("AmazonS3", "TimedStorage-ByteHrs", "", {}),
        "logs_ingestion_gb": ("AmazonCloudWatch", "DataProcessing-Bytes", "", {}),
        "secret_month": ("AWSSecretsManager", "AWSSecretsManager-Secrets", "", {}),
        "kms_key_month": ("awskms", "KMS-Keys", "", {}),
    }
    for name in AURORA_CLASSES:
        found[f"aurora_{name}_hour"] = (
            "AmazonRDS",
            f"InstanceUsage:{name}",
            "Aurora PostgreSQL",
            {},
        )
    for name in RDS_CLASSES:
        found[f"rds_{name}_hour"] = (
            "AmazonRDS",
            f"InstanceUsage:{name}",
            "running PostgreSQL",
            {},
        )
        found[f"rds_{name}_multi_az_hour"] = (
            "AmazonRDS",
            f"Multi-AZUsage:{name}",
            "running PostgreSQL",
            {},
        )
    for name in CACHE_NODES:
        found[f"valkey_{name}_hour"] = (
            "AmazonElastiCache",
            f"NodeUsage:{name}",
            "Valkey",
            {},
        )
    return found


def products(service: str, filters: dict[str, str]) -> list[dict]:
    command = [
        "aws",
        "pricing",
        "get-products",
        "--region",
        "us-east-1",
        "--service-code",
        service,
        "--output",
        "json",
        "--filters",
    ] + [f"Type=TERM_MATCH,Field={k},Value={v}" for k, v in filters.items()]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    return [json.loads(item) for item in json.loads(result.stdout)["PriceList"]]


def first_price(items: list[dict], description: str) -> float | None:
    """The first non-zero on-demand price (lowest tier) whose text matches."""
    prices: list[tuple[float, float]] = []
    for item in items:
        for term in item.get("terms", {}).get("OnDemand", {}).values():
            for dim in term["priceDimensions"].values():
                if description and description not in dim["description"]:
                    continue
                usd = float(dim["pricePerUnit"].get("USD", "0"))
                if usd > 0:
                    prices.append((float(dim.get("beginRange", "0")), usd))
    return min(prices)[1] if prices else None


def region_prices(region: str, code: str) -> dict[str, float]:
    found: dict[str, float] = {}
    for key, (service, usage, description, extra) in specs().items():
        # Large classes are abbreviated in usage types: db.r7g.xlarge -> db.r7g.xl.
        short = usage.replace("xlarge", "xl")
        forms = dict.fromkeys([usage, short])
        candidates = [f"{c}-{u}" for u in forms for c in (code, region)] + list(forms)
        for candidate in candidates:
            filters = {"regionCode": region, "usagetype": candidate, **extra}
            price = first_price(products(service, filters), description)
            if price is not None:
                found[key] = price
                break
        else:
            print(f"  {region}: no price for {key}", file=sys.stderr)
    return found


def main(argv: list[str]) -> int:
    wanted = argv or list(REGIONS)
    current = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {"regions": {}}
    for region in wanted:
        print(f"{region} …", file=sys.stderr)
        current["regions"][region] = region_prices(region, REGIONS[region])
    current["date"] = datetime.date.today().isoformat()
    current["source"] = "AWS Price List API, on-demand, USD"
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUTPUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
