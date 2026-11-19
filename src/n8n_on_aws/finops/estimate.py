"""Monthly cost estimate of a deployment, before anything is deployed.

Pure functions from the configuration and the plan to line items, with the prices
of ``prices.json`` (AWS Price List API, refreshed by ``scripts/refresh_prices.py``).
Each line has a **fixed** part, what the deployment costs at its minimum size and
with no traffic, and a **usage** part that follows the traffic assumptions of
``finops.usage`` (profile defaults when unset). Free tiers are shown in the notes,
never subtracted silently, except CloudFront's, which is always free for the first
1 TB and 10 million requests a month.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from pathlib import Path

from pydantic import BaseModel, Field

from ..config import instances as ec2
from ..config.models import CreatedVpc, Deployment
from ..plan import Plan

HOURS = 730.0  # hours in an average month
PRICES = Path(__file__).parent / "prices.json"
FALLBACK_REGION = "us-east-1"
SPOT_FACTOR = 0.30  # Fargate Spot and EC2 Spot run up to 70% below on-demand
REDIS_FACTOR = {"serverless": 1.49, "nodes": 1.25}  # Redis OSS over Valkey
NAT_SHARE = 0.2  # share of data out that workflows send through the NAT
EDITOR_SHARE = 0.3  # share of requests that hit the editor (login wall)

# Global list prices that do not vary by region (USD, 2026-11-19).
GLOBAL = {
    "dashboard_month": 3.0,  # the first 3 dashboards of an account are free
    "alarm_month": 0.10,
    "custom_metric_month": 0.30,
    "waf_acl_month": 5.0,
    "waf_rule_month": 1.0,
    "waf_million_requests": 0.60,
    "lambda_edge_million_requests": 0.60,
    "lambda_edge_gb_second": 0.00005001,
    "cloudfront_gb": 0.085,  # North America and Europe, first tier
    "cloudfront_10k_requests": 0.012,  # HTTPS
    "internet_gb": 0.09,  # data transfer out, first tier
}
CLOUDFRONT_FREE_GB = 1024.0
CLOUDFRONT_FREE_MILLION_REQUESTS = 10.0
INTERNET_FREE_GB = 100.0
VALKEY_SERVERLESS_MIN_GB = 0.1  # ElastiCache Serverless bills at least 100 MB
IDLE_ECPU_MILLIONS = 7.0  # measured: n8n's idle polling on Serverless Valkey

# Traffic assumptions by profile, used when finops.usage leaves them unset.
PROFILE_USAGE: dict[str, dict[str, float]] = {
    "nano": {"requests_millions": 0.2, "data_out_gb": 2, "data_gb": 1, "logs_gb": 0.5},
    "small": {"requests_millions": 1, "data_out_gb": 10, "data_gb": 5, "logs_gb": 2},
    "medium": {"requests_millions": 5, "data_out_gb": 50, "data_gb": 20, "logs_gb": 10},
    "large": {
        "requests_millions": 20,
        "data_out_gb": 200,
        "data_gb": 100,
        "logs_gb": 40,
    },
    "xlarge": {
        "requests_millions": 100,
        "data_out_gb": 1000,
        "data_gb": 500,
        "logs_gb": 150,
    },
}


class LineItem(BaseModel):
    component: str
    item: str
    fixed: float = 0.0
    usage: float = 0.0
    note: str = ""


class Estimate(BaseModel):
    region: str
    priced_in: str
    prices_date: str
    hours: float
    assumptions: dict[str, float]
    items: list[LineItem] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def fixed(self) -> float:
        return sum(i.fixed for i in self.items)

    @property
    def usage(self) -> float:
        return sum(i.usage for i in self.items)

    @property
    def total(self) -> float:
        return self.fixed + self.usage

    def by_component(self) -> dict[str, tuple[float, float]]:
        found: dict[str, tuple[float, float]] = {}
        for item in self.items:
            fixed, usage = found.get(item.component, (0.0, 0.0))
            found[item.component] = (fixed + item.fixed, usage + item.usage)
        return found


class PriceTable(BaseModel):
    date: str = "unknown"
    source: str = ""
    regions: dict[str, dict[str, float]]


@cache
def price_table() -> PriceTable:
    return PriceTable.model_validate_json(PRICES.read_text(encoding="utf-8"))


def _hours_on(d: Deployment) -> float:
    """Hours a month the n8n services run (office-hours schedules stop them)."""
    schedule = d.finops.schedule
    if schedule.kind != "office-hours":
        return HOURS
    days = [x.strip() for x in schedule.weekdays.upper().split(",")]
    week = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    count = 0
    for part in days:
        if "-" in part:
            first, last = part.split("-")
            count += week.index(last) - week.index(first) + 1
        else:
            count += 1
    start_h, start_m = (int(x) for x in schedule.start.split(":"))
    stop_h, stop_m = (int(x) for x in schedule.stop.split(":"))
    per_day = max(0.0, (stop_h * 60 + stop_m - start_h * 60 - start_m) / 60)
    return count * per_day * 52 / 12


def estimate(d: Deployment, plan: Plan) -> Estimate:
    """The monthly estimate of ``d``, from its resolved ``plan``."""
    table = price_table()
    regions = table.regions
    region = d.aws.region or FALLBACK_REGION
    priced_in = region if region in regions else FALLBACK_REGION
    prices = regions[priced_in]
    usage = d.finops.usage
    profile = PROFILE_USAGE[d.profile]

    def assume(name: str) -> float:
        value = getattr(usage, name)
        return float(profile[name] if value is None else value)

    hours = HOURS
    on = _hours_on(d)
    result = Estimate(
        region=region,
        priced_in=priced_in,
        prices_date=table.date,
        hours=on,
        assumptions={
            "requests_millions": assume("requests_millions"),
            "data_out_gb": assume("data_out_gb"),
            "data_gb": assume("data_gb"),
            "logs_gb": assume("logs_gb"),
        },
    )
    if priced_in != region:
        result.notes.append(
            f"no price table for {region}: priced as {priced_in}; refresh "
            "prices.json with scripts/refresh_prices.py"
        )
    add = result.items.append
    a = result.assumptions

    def price(key: str) -> float | None:
        value = prices.get(key)
        if value is None:
            result.notes.append(f"no price for {key} in {priced_in}: not counted")
        return value

    # ------------------------------------------------------------ compute
    if d.runtime.ecs.capacity == "ec2":
        result.items.extend(
            _ec2_compute(d, price, on, average_workers=usage.average_workers)
        )
    else:
        result.items.extend(_fargate_compute(d, plan, price, on))

    # ------------------------------------------------------------ network
    ipv4 = price("public_ipv4_hour") or 0.0
    vpc = d.network.vpc if isinstance(d.network.vpc, CreatedVpc) else None
    azs = vpc.azs if vpc is not None else 2
    task_ips = vpc is not None and vpc.nat == "none"
    if task_ips:
        running = sum(r.replicas_min for r in plan.roles)
        add(
            LineItem(
                component="network",
                item=f"public IPv4 of {running} task(s)",
                fixed=running * ipv4 * on,
                note="tasks get public addresses when the VPC has no NAT",
            )
        )
    if vpc is not None and vpc.nat in ("single", "per-az"):
        count = 1 if vpc.nat == "single" else azs
        nat_hour = price("nat_gateway_hour") or 0.0
        nat_gb = price("nat_gateway_gb") or 0.0
        add(
            LineItem(
                component="network",
                item=f"{count} NAT gateway(s) and their IPv4",
                fixed=count * (nat_hour + ipv4) * hours,
                usage=a["data_out_gb"] * NAT_SHARE * nat_gb,
                note=f"{NAT_SHARE:.0%} of data out assumed through the NAT",
            )
        )
    if vpc is not None and vpc.nat == "instance":
        add(
            LineItem(
                component="network",
                item="NAT instance t4g.nano and its IPv4",
                fixed=((price("ec2_t4g_nano_hour") or 0.0) + ipv4) * hours,
            )
        )
    if vpc is not None:
        interface = [e for e in vpc.endpoints if e != "s3"]
        if interface:
            add(
                LineItem(
                    component="network",
                    item=f"{len(interface)} interface endpoint(s) × {azs} AZ",
                    fixed=len(interface)
                    * azs
                    * (price("vpc_endpoint_hour") or 0.0)
                    * hours,
                )
            )

    # ------------------------------------------------------------ ingress
    requests = a["requests_millions"]
    data_out = a["data_out_gb"]
    if d.has_alb:
        ips = azs if d.ingress.kind == "alb" and d.ingress.public else 0
        lcu_hours = requests * 1e6 / 90_000  # 25 new connections a second per LCU
        add(
            LineItem(
                component="ingress",
                item="Application Load Balancer"
                + (f" and {ips} public IPv4" if ips else ""),
                fixed=((price("alb_hour") or 0.0) + ips * ipv4) * hours,
                usage=lcu_hours * (price("alb_lcu_hour") or 0.0),
                note="LCUs from the request assumption",
            )
        )
    if d.ingress.kind == "cloudfront":
        paid_gb = max(0.0, data_out - CLOUDFRONT_FREE_GB)
        paid_requests = max(0.0, requests - CLOUDFRONT_FREE_MILLION_REQUESTS)
        add(
            LineItem(
                component="ingress",
                item="CloudFront",
                usage=paid_gb * GLOBAL["cloudfront_gb"]
                + paid_requests * 100 * GLOBAL["cloudfront_10k_requests"],
                note="free up to 1 TB and 10 million requests a month",
            )
        )
    else:
        add(
            LineItem(
                component="ingress",
                item="data transfer out to the internet",
                usage=max(0.0, data_out - INTERNET_FREE_GB) * GLOBAL["internet_gb"],
                note="the first 100 GB a month are free across the account",
            )
        )

    # ------------------------------------------------------------- edge
    if d.auth.mode == "cloudfront-cognito":
        editor = requests * EDITOR_SHARE
        add(
            LineItem(
                component="edge",
                item="Lambda@Edge login wall",
                usage=editor * GLOBAL["lambda_edge_million_requests"]
                + editor * 1e6 * 0.128 * 0.05 * GLOBAL["lambda_edge_gb_second"],
                note=f"{EDITOR_SHARE:.0%} of requests reach the editor; Cognito is "
                "free up to 10,000 monthly active users",
            )
        )
    acls: list[tuple[str, str]] = []
    if d.ingress.kind == "cloudfront" and (
        d.ingress.cloudfront.waf.kind == "managed" or d.auth.mode == "ip-allowlist"
    ):
        acls.append(("CloudFront", "CLOUDFRONT"))
    if d.has_alb and d.ingress.waf.kind == "managed":
        acls.append(("ALB", "REGIONAL"))
    if acls:
        from ..cdk.waf import rule_names

        for title, kind in acls:
            rules = len(rule_names(d, kind))  # type: ignore[arg-type]
            add(
                LineItem(
                    component="edge",
                    item=f"AWS WAF on {title}: web ACL and {rules} rule(s)",
                    fixed=GLOBAL["waf_acl_month"] + rules * GLOBAL["waf_rule_month"],
                    usage=requests * GLOBAL["waf_million_requests"],
                )
            )

    # ----------------------------------------------------------- database
    db = d.database
    data_gb = a["data_gb"]
    if db.kind == "aurora-serverless":
        floor = max(db.min_acu, 0.5)
        # n8n keeps a connection open, so Aurora only pauses when n8n is stopped.
        db_hours = on if db.min_acu == 0 else hours
        mean = usage.average_acu if usage.average_acu is not None else floor
        mean = min(max(mean, floor), db.max_acu)
        instances = 1 + db.readers
        acu = price("aurora_acu_hour") or 0.0
        result.assumptions["average_acu"] = mean
        add(
            LineItem(
                component="database",
                item=f"Aurora Serverless v2, {instances} instance(s) at {mean:g} ACU",
                fixed=instances * floor * acu * db_hours,
                usage=instances * (mean - floor) * acu * db_hours,
                note="0.5 ACU at least while n8n runs; pauses only when it is stopped"
                if db.min_acu == 0
                else f"{db.min_acu:g} to {db.max_acu:g} ACU",
            )
        )
    elif db.kind == "aurora":
        aurora_hourly = price(f"aurora_{db.instance_class}_hour")
        add(
            LineItem(
                component="database",
                item=f"Aurora {db.instance_class} × {db.instances}",
                fixed=(aurora_hourly or 0.0) * db.instances * hours,
                note="I/O-Optimized adds about 30% to instances"
                if db.io_optimized
                else "",
            )
        )
    elif db.kind == "rds":
        suffix = "_multi_az" if db.multi_az else ""
        rds_hourly = price(f"rds_{db.instance_class}{suffix}_hour")
        storage = price(f"rds_gp3{suffix}_gb_month") or 0.0
        add(
            LineItem(
                component="database",
                item=f"RDS PostgreSQL {db.instance_class}"
                + (" Multi-AZ" if db.multi_az else "")
                + f", {db.storage_gb} GB gp3",
                fixed=(rds_hourly or 0.0) * hours + db.storage_gb * storage,
            )
        )
    elif db.kind == "external":
        result.notes.append("the external database is not counted")
    if db.kind in ("aurora-serverless", "aurora"):
        add(
            LineItem(
                component="database",
                item="Aurora storage and I/O",
                usage=data_gb * (price("aurora_storage_gb_month") or 0.0)
                + data_gb * 1e6 * (price("aurora_io") or 0.0),
                note="one million I/O a month per GB stored is assumed",
            )
        )

    # -------------------------------------------------------------- queue
    queue = d.queue
    if d.is_queued and queue.kind == "elasticache-serverless":
        factor = REDIS_FACTOR["serverless"] if queue.engine == "redis" else 1.0
        add(
            LineItem(
                component="queue",
                item=f"ElastiCache Serverless ({queue.engine})",
                fixed=factor
                * (
                    VALKEY_SERVERLESS_MIN_GB
                    * (price("valkey_serverless_gb_hour") or 0.0)
                    * hours
                    + IDLE_ECPU_MILLIONS
                    * 1e6
                    * (price("valkey_serverless_ecpu") or 0.0)
                ),
                note="100 MB minimum and n8n's idle polling; jobs add little",
            )
        )
    elif d.is_queued and queue.kind == "elasticache":
        factor = REDIS_FACTOR["nodes"] if queue.engine == "redis" else 1.0
        nodes = 1 + queue.replicas
        add(
            LineItem(
                component="queue",
                item=f"ElastiCache {queue.node_type} × {nodes} ({queue.engine})",
                fixed=factor
                * nodes
                * (price(f"valkey_{queue.node_type}_hour") or 0.0)
                * hours,
            )
        )
    elif d.is_queued and queue.kind == "external":
        result.notes.append("the external Redis is not counted")

    # ------------------------------------------------------------ storage
    if d.needs_efs:
        add(
            LineItem(
                component="storage",
                item="EFS (n8n folder)",
                usage=(data_gb if db.kind == "sqlite" else 1.0)
                * (price("efs_standard_gb_month") or 0.0),
            )
        )
    if d.storage.bucket.kind == "create":
        add(
            LineItem(
                component="storage",
                item="S3 bucket",
                usage=data_gb * (price("s3_standard_gb_month") or 0.0),
            )
        )
    if d.backup.kind == "aws-backup":
        result.notes.append(
            "AWS Backup storage beyond the database size is not counted"
        )

    # ----------------------------------------------------------- security
    secrets = 0
    if d.n8n.encryption_key.kind != "existing":
        secrets += 1
    if d.n8n.runners.mode == "external":
        secrets += 1
    if db.kind in ("aurora-serverless", "aurora", "rds"):
        secrets += 1
    kms_key = d.aws.kms_key.kind == "create"
    add(
        LineItem(
            component="security",
            item=f"{secrets} secret(s)" + (" and a KMS key" if kms_key else ""),
            fixed=secrets * (price("secret_month") or 0.0)
            + (price("kms_key_month") or 0.0 if kms_key else 0.0),
        )
    )

    # ------------------------------------------------------ observability
    alarms = _alarm_count(d, plan)
    dashboard = GLOBAL["dashboard_month"] if d.observability.dashboard.enabled else 0
    metrics = 0
    if d.collects_metrics:
        metrics = (4 if d.is_queued else 0) + (
            5 if d.observability.prometheus == "cloudwatch" else 0
        )
    add(
        LineItem(
            component="observability",
            item=f"dashboard, about {alarms} alarms, {metrics} n8n metric(s)",
            fixed=dashboard
            + alarms * GLOBAL["alarm_month"]
            + metrics * GLOBAL["custom_metric_month"],
            note="the first 3 dashboards of an account are free",
        )
    )
    add(
        LineItem(
            component="observability",
            item="CloudWatch Logs ingestion",
            usage=a["logs_gb"] * (price("logs_ingestion_gb") or 0.0),
        )
    )
    if d.observability.container_insights:
        result.notes.append(
            "Container Insights (enhanced) is billed per observation: not counted"
        )
    if on < HOURS:
        result.notes.append(
            f"n8n runs {on:.0f} of {HOURS:.0f} hours a month (office hours)"
        )
    return result


def _alarm_count(d: Deployment, plan: Plan) -> int:
    """About as many alarms as the stacks create, scaling alarms included."""
    count = 1  # main CPU
    if d.finops.schedule.kind != "office-hours":
        count += 1  # main down
    if d.has_alb:
        count += 1 + (2 if d.topology.mode == "split" else 1)  # 5xx, unhealthy
    if d.database.kind in ("aurora-serverless", "aurora", "rds"):
        count += 1 if d.database.kind == "aurora-serverless" else 2
    if d.is_queued and d.queue.kind in ("elasticache-serverless", "elasticache"):
        count += 1 if d.queue.kind == "elasticache-serverless" else 2
    if d.needs_efs:
        count += 1
    if d.network.vpc.kind == "create" and d.network.vpc.nat != "none":
        count += 1
    if d.ingress.kind == "cloudfront" and d.ingress.cloudfront.origin == "task":
        count += 1  # origin updater
    for role in plan.roles:
        if role.replicas_min == role.replicas_max:
            continue
        if role.name == "webhook":
            count += 2
        elif role.name == "worker" and d.scales_on_queue:
            count += 1 + 2 + 1  # backlog, two step policies, CPU without scale-in
        elif role.name == "worker":
            count += 2
    return count


def _fargate_compute(
    d: Deployment, plan: Plan, price: Callable[[str], float | None], on: float
) -> list[LineItem]:
    """Fargate tasks: each role at its minimum, plus the average extra workers."""
    found: list[LineItem] = []
    usage = d.finops.usage
    arm = d.runtime.ecs.architecture == "arm64"
    vcpu = price("fargate_arm_vcpu_hour" if arm else "fargate_vcpu_hour") or 0.0
    gb = price("fargate_arm_gb_hour" if arm else "fargate_gb_hour") or 0.0
    capacity_spot = d.runtime.ecs.capacity == "fargate-spot"
    average_workers = usage.average_workers
    for role in plan.roles:
        hourly = role.cpu / 1024 * vcpu + role.memory / 1024 * gb
        low = role.replicas_min
        mean = float(low)
        if role.name == "worker" and average_workers is not None:
            mean = max(float(low), min(average_workers, float(role.replicas_max)))
        spot_workers = role.name == "worker" and d.topology.workers.spot
        if capacity_spot:
            factor_fixed = factor_extra = SPOT_FACTOR
        elif spot_workers:
            # One on-demand task first, then Spot (see the capacity strategy).
            on_demand = 1 if low >= 1 else 0
            factor_fixed = (
                (on_demand + (low - on_demand) * SPOT_FACTOR) / low if low else 1.0
            )
            factor_extra = SPOT_FACTOR
        else:
            factor_fixed = factor_extra = 1.0
        spot_note = " (Spot priced 70% below on-demand)" if factor_extra < 1 else ""
        found.append(
            LineItem(
                component="compute",
                item=f"Fargate {role.name}: {role.cpu / 1024:g} vCPU, "
                f"{role.memory / 1024:g} GB × {mean:g}",
                fixed=low * hourly * on * factor_fixed,
                usage=(mean - low) * hourly * on * factor_extra,
                note=f"{low} to {role.replicas_max} tasks{spot_note}",
            )
        )
    return found


def _ec2_compute(
    d: Deployment,
    price: Callable[[str], float | None],
    on: float,
    average_workers: float | None,
) -> list[LineItem]:
    """ECS hosts: the instances that hold the tasks, and their root volumes.

    The on-demand group never goes below ``ec2.min`` instances, even when an
    office-hours schedule stops the services; above that, instances follow the
    tasks. Instance counts come from the first instance type of the list.
    """
    spec = d.runtime.ecs.ec2
    name = spec.instance_types[0]
    instance = ec2.spec(name)
    hourly = price(f"ec2_{name}_hour")
    if instance is None or hourly is None:
        return [
            LineItem(
                component="compute",
                item=f"EC2 hosts {name}",
                note="no size or price for this type: not counted",
            )
        ]
    disk = (price("ebs_gp3_gb_month") or 0.0) * spec.root_volume_gib
    groups = ec2.placement(d)
    if average_workers is not None:
        for tasks in groups.values():
            for x in tasks:
                if x.path == "topology.workers":
                    extra = max(0.0, average_workers - d.topology.workers.min)
                    x.high = min(x.high, x.low + int(round(extra)))
    found: list[LineItem] = []
    for kind, tasks in groups.items():
        spot = kind == "spot"
        if not tasks and spot:
            continue
        floor = spec.min if not spot else 0
        low = max(floor, ec2.instances_for(tasks, instance, high=False))
        mean = max(low, ec2.instances_for(tasks, instance, high=True))
        if average_workers is None:
            mean = low
        factor = SPOT_FACTOR if spot else 1.0
        fixed = (floor * HOURS + (low - floor) * on) * (hourly * factor + disk / HOURS)
        found.append(
            LineItem(
                component="compute",
                item=f"EC2 {kind} hosts: {name} × {mean:g}",
                fixed=fixed,
                usage=(mean - low) * on * (hourly * factor + disk / HOURS),
                note=f"{low} instance(s) at the minimum, "
                f"{spec.root_volume_gib} GB gp3 each"
                + (" (Spot priced 70% below on-demand)" if spot else ""),
            )
        )
    return found


def compute_floor(d: Deployment, plan: Plan) -> tuple[float, float]:
    """Monthly compute floor of ``d`` on EC2 hosts and on Fargate, in USD."""
    table = price_table()
    region = d.aws.region or FALLBACK_REGION
    prices = table.regions.get(region) or table.regions[FALLBACK_REGION]

    def price(key: str) -> float | None:
        return prices.get(key)

    on = _hours_on(d)
    on_ec2 = sum(i.fixed for i in _ec2_compute(d, price, on, None))
    fargate = d.model_copy(deep=True)
    fargate.runtime.ecs.capacity = "fargate"
    on_fargate = sum(i.fixed for i in _fargate_compute(fargate, plan, price, on))
    return on_ec2, on_fargate
