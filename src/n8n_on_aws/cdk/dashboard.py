"""One CloudWatch dashboard and the alarms of a deployment.

The dashboard is generated from what the stacks actually contain, so it follows the
configuration: each resource that exists adds its section, and an absent resource
adds nothing. Resources outside AWS get a text widget that says so. The alarms are
always created, because the dashboard shows their status; they notify only when
``observability.alarms.email`` is set.

:func:`unmonitored` lists the resources that have no widget, so a resource added to a
stack without a section fails the tests instead of going unwatched.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_rds as rds
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from constructs import IConstruct

from ..config.models import Deployment
from ..plan import Plan
from .edge import EDGE_REGION

if TYPE_CHECKING:
    from .data import DataStack
    from .network import NetworkStack
    from .service import ServiceStack

WIDTH = 24

# Resource types an operator watches. Each one in a template needs a widget.
MONITORED_TYPES = frozenset(
    {
        "AWS::CloudFront::Distribution",
        "AWS::EC2::Instance",
        "AWS::EC2::NatGateway",
        "AWS::ECS::Service",
        "AWS::EFS::FileSystem",
        "AWS::ElastiCache::ReplicationGroup",
        "AWS::ElastiCache::ServerlessCache",
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "AWS::Lambda::Function",
        "AWS::RDS::DBCluster",
        "AWS::RDS::DBInstance",
        "AWS::S3::Bucket",
    }
)

# CDK's own helper functions: they run once at deployment, not in operation.
FRAMEWORK_MARKERS = (
    "AWS679f53fac002430cb0da5b7982bd2287",  # AwsCustomResource singleton
    "Custom::VpcRestrictDefaultSGCustomResourceProvider",
    "Custom::S3AutoDeleteObjectsCustomResourceProvider",
    "/framework-onEvent",  # custom resource Provider framework
    "LogRetention",
)

# Section order on the dashboard.
HEADER, HEALTH, INGRESS, ROLES, RUNNERS, LOGS = 0, 10, 20, 30, 35, 40
DATABASE, QUEUE, FILES, STORAGE, NETWORK, GLUE, COST = 50, 60, 70, 80, 90, 100, 110

_NOT_MONITORED = "is outside this deployment and is not monitored here."


class _Section:
    """A titled group of widget rows, and the constructs it watches."""

    def __init__(
        self, order: int, title: str, rows: list[list[cloudwatch.IWidget]]
    ) -> None:
        self.order = order
        self.title = title
        self.rows = rows


def _range(value: str) -> cdk.Duration:
    """``3h`` → a Duration (m, h, d or w)."""
    match = re.fullmatch(r"(\d+)([mhdw])", value)
    assert match is not None
    amount, unit = int(match.group(1)), match.group(2)
    if unit == "m":
        return cdk.Duration.minutes(amount)
    if unit == "h":
        return cdk.Duration.hours(amount)
    return cdk.Duration.days(amount * (7 if unit == "w" else 1))


def _text(markdown: str, height: int = 2) -> cloudwatch.TextWidget:
    return cloudwatch.TextWidget(markdown=markdown, width=WIDTH, height=height)


class DashboardBuilder:
    """Collects sections and alarms from the stacks, then builds them."""

    def __init__(
        self,
        d: Deployment,
        plan: Plan,
        network: NetworkStack,
        data: DataStack,
        service: ServiceStack,
    ) -> None:
        self.d = d
        self.plan = plan
        self.network = network
        self.data = data
        self.service = service
        self.region = service.region
        self.period = cdk.Duration.minutes(d.observability.dashboard.period_minutes)
        self.sections: list[_Section] = []
        self.alarms: list[cloudwatch.Alarm] = []
        self.covered: set[str] = set()
        self.dashboard: cloudwatch.Dashboard | None = None

    # ------------------------------------------------------------ plumbing

    def add(
        self,
        order: int,
        title: str,
        rows: list[list[cloudwatch.IWidget]],
        covers: Sequence[IConstruct] = (),
    ) -> None:
        """Register a section and the constructs it watches."""
        self.sections.append(_Section(order, title, rows))
        for construct in covers:
            self.covered.add(construct.node.path)

    def graphs(
        self,
        specs: Sequence[tuple[str, Sequence[cloudwatch.IMetric]]],
        height: int = 6,
        **options: Any,
    ) -> list[cloudwatch.IWidget]:
        """One row of graphs of equal width."""
        width = WIDTH // len(specs)
        options.setdefault("period", self.period)
        return [
            cloudwatch.GraphWidget(
                title=title, left=list(metrics), width=width, height=height, **options
            )
            for title, metrics in specs
        ]

    def alarm(
        self,
        construct_id: str,
        metric: cloudwatch.IMetric,
        threshold: float,
        description: str,
        *,
        evaluation_periods: int = 3,
        comparison: cloudwatch.ComparisonOperator = (
            cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
        ),
        missing: cloudwatch.TreatMissingData = (
            cloudwatch.TreatMissingData.NOT_BREACHING
        ),
    ) -> None:
        """Declare an alarm; it is created in the n8n stack."""
        self.alarms.append(
            cloudwatch.Alarm(
                self.service,
                construct_id,
                alarm_name=f"{self.d.name}-{_kebab(construct_id)}",
                metric=metric,
                threshold=threshold,
                evaluation_periods=evaluation_periods,
                comparison_operator=comparison,
                treat_missing_data=missing,
                alarm_description=description,
            )
        )

    def console(self, path: str) -> str:
        """A link to the AWS console of this deployment's region."""
        region = self.region
        return f"https://{region}.console.aws.amazon.com/{path}?region={region}"

    # ---------------------------------------------------------------- build

    def build(self) -> None:
        """Register every section, create the alarms and the dashboard."""
        self._header()
        self._network()
        self._database()
        self._queue()
        self._files()
        self._storage()
        self._ingress()
        self._roles()
        self._runners()
        self._logs()
        self._glue()
        self._cost()
        self._notify()
        self._health()
        spec = self.d.observability.dashboard
        if not spec.enabled:
            return
        dashboard = cloudwatch.Dashboard(
            self.service,
            "Dashboard",
            dashboard_name=self.d.name,
            default_interval=_range(spec.range),
        )
        for section in sorted(self.sections, key=lambda s: s.order):
            dashboard.add_widgets(_text(f"## {section.title}", height=1))
            for row in section.rows:
                dashboard.add_widgets(*row)
        self.dashboard = dashboard
        cdk.CfnOutput(
            self.service,
            "DashboardUrl",
            value=self.console("cloudwatch/home")
            + f"#dashboards/dashboard/{self.d.name}",
            description="CloudWatch dashboard of this deployment",
        )

    @property
    def titles(self) -> list[str]:
        return [s.title for s in sorted(self.sections, key=lambda s: s.order)]

    def _notify(self) -> None:
        emails = self.d.observability.alarms.email
        if not emails or not self.alarms:
            return
        topic = sns.Topic(
            self.service, "Alarms", display_name=f"n8n {self.d.name} alarms"
        )
        for email in emails:
            topic.add_subscription(subscriptions.EmailSubscription(email))
        action = cw_actions.SnsAction(topic)
        for alarm in self.alarms:
            alarm.add_alarm_action(action)
            alarm.add_ok_action(action)

    # ------------------------------------------------------------- sections

    def _header(self) -> None:
        d, plan, svc = self.d, self.plan, self.service
        # The console wants the literal name, double-encoded; the token would not do.
        log_group = quote(quote(f"/n8n-on-aws/{d.name}", safe=""), safe="")
        cluster = self.console(f"ecs/v2/clusters/{svc.cluster.cluster_name}/services")
        links = [
            f"[Open n8n]({svc.url})",
            f"[Logs]({self.console('cloudwatch/home')}"
            f"#logsV2:log-groups/log-group/{log_group.replace('%', '$')})",
            f"[ECS cluster]({cluster})",
        ]
        if self.data.db_construct is not None:
            links.append(f"[Database]({self.console('rds/home')}#databases:)")
        purpose = f" ({plan.purpose})" if plan.purpose else ""
        self.add(
            HEADER,
            f"n8n {d.name}",
            [
                [
                    _text(
                        f"**{plan.edition}** edition{purpose} · n8n `{d.n8n.version}`"
                        f" · {d.environment} · profile {d.profile} · {plan.platform}"
                        f" · topology {d.topology.mode}\n\n" + " · ".join(links)
                    )
                ]
            ],
        )

    def _health(self) -> None:
        d, svc = self.d, self.service
        rows: list[list[cloudwatch.IWidget]] = []
        if self.alarms:
            rows.append(
                [
                    cloudwatch.AlarmStatusWidget(
                        title="Alarms",
                        alarms=self.alarms,
                        width=WIDTH,
                        height=2 + (len(self.alarms) + 3) // 4,
                        sort_by=cloudwatch.AlarmStatusWidgetSortBy.STATE_UPDATED_TIMESTAMP,
                    )
                ]
            )
        insights = (
            d.observability.container_insights
            and d.runtime.ecs.cluster.kind == "create"
        )
        if insights:
            specs = []
            for role in self.plan.roles:
                service = svc.services[role.name]
                dims = {
                    "ClusterName": svc.cluster.cluster_name,
                    "ServiceName": service.service_name,
                }
                specs.append(
                    (
                        f"{role.name} tasks (running / desired)",
                        [
                            cloudwatch.Metric(
                                namespace="ECS/ContainerInsights",
                                metric_name=name,
                                dimensions_map=dims,
                                statistic="Average",
                                label=label,
                            )
                            for name, label in (
                                ("RunningTaskCount", "running"),
                                ("DesiredTaskCount", "desired"),
                            )
                        ],
                    )
                )
            rows.append(self.graphs(specs, height=5))
        else:
            rows.append(
                [
                    _text(
                        "Task counts per role need Container Insights: set "
                        "`observability.container_insights: true` (billed per "
                        "metric and per GB of performance logs).",
                        height=1,
                    )
                ]
            )
        self.add(HEALTH, "Health", rows)

    def _ingress(self) -> None:
        svc = self.service
        distribution = svc.distribution
        if distribution is not None:
            dims = {"DistributionId": distribution.distribution_id, "Region": "Global"}

            def edge(name: str, statistic: str, label: str) -> cloudwatch.Metric:
                return cloudwatch.Metric(
                    namespace="AWS/CloudFront",
                    metric_name=name,
                    dimensions_map=dims,
                    statistic=statistic,
                    label=label,
                    region=EDGE_REGION,
                )

            self.add(
                INGRESS,
                "Ingress: CloudFront",
                [
                    self.graphs(
                        [
                            ("Requests", [edge("Requests", "Sum", "requests")]),
                            (
                                "Error rate (%)",
                                [
                                    edge("4xxErrorRate", "Average", "4xx"),
                                    edge("5xxErrorRate", "Average", "5xx"),
                                ],
                            ),
                            (
                                "Bytes downloaded",
                                [edge("BytesDownloaded", "Sum", "bytes")],
                            ),
                        ],
                        region=EDGE_REGION,
                    )
                ],
                covers=[distribution],
            )
        alb = svc.alb
        if alb is None:
            return
        target = elbv2.HttpCodeTarget
        health: list[cloudwatch.IMetric] = []
        for name, group in svc.target_groups.items():
            health.append(group.metrics.healthy_host_count(label=f"{name} healthy"))
            health.append(group.metrics.unhealthy_host_count(label=f"{name} unhealthy"))
        self.add(
            INGRESS,
            "Ingress: load balancer",
            [
                self.graphs(
                    [
                        (
                            "Requests",
                            [
                                alb.metrics.request_count(label="requests"),
                                alb.metrics.rejected_connection_count(
                                    label="rejected connections"
                                ),
                            ],
                        ),
                        (
                            "Errors",
                            [
                                alb.metrics.http_code_target(
                                    target.TARGET_4XX_COUNT, label="target 4xx"
                                ),
                                alb.metrics.http_code_target(
                                    target.TARGET_5XX_COUNT, label="target 5xx"
                                ),
                                alb.metrics.http_code_elb(
                                    elbv2.HttpCodeElb.ELB_5XX_COUNT, label="ALB 5xx"
                                ),
                            ],
                        ),
                        (
                            "Response time (s)",
                            [
                                alb.metrics.target_response_time(
                                    statistic="p50", label="p50"
                                ),
                                alb.metrics.target_response_time(
                                    statistic="p99", label="p99"
                                ),
                            ],
                        ),
                        ("Targets", health),
                    ]
                )
            ],
            covers=[alb],
        )
        self.alarm(
            "Errors5xx",
            alb.metrics.http_code_target(
                target.TARGET_5XX_COUNT, period=cdk.Duration.minutes(5)
            ),
            10,
            "n8n returned more than 10 errors in 5 minutes",
            evaluation_periods=1,
        )
        for name, group in svc.target_groups.items():
            self.alarm(
                f"{name.capitalize()}Unhealthy",
                group.metrics.unhealthy_host_count(
                    statistic="Maximum", period=cdk.Duration.minutes(5)
                ),
                0,
                f"an n8n {name} target has been unhealthy for 15 minutes",
            )

    def _roles(self) -> None:
        d, svc = self.d, self.service
        rows: list[list[cloudwatch.IWidget]] = []
        services = []
        for role in self.plan.roles:
            service = svc.services[role.name]
            services.append(service)
            spot = role.name == "worker" and d.topology.workers.spot
            capacity = "Spot after 1 on-demand" if spot else d.runtime.ecs.capacity
            replicas = (
                f"{role.replicas_min}"
                if role.replicas_min == role.replicas_max
                else f"{role.replicas_min} to {role.replicas_max}"
            )
            rows.append(
                [
                    _text(
                        f"**{role.name}** · `{role.command}` · {replicas} tasks · "
                        f"{role.cpu} CPU units, {role.memory} MiB · {capacity} · "
                        f"scaling: {role.scaling}",
                        height=1,
                    )
                ]
            )
            rows.append(
                self.graphs(
                    [
                        (
                            f"{role.name} CPU (%)",
                            [
                                service.metric_cpu_utilization(label="average"),
                                service.metric_cpu_utilization(
                                    statistic="Maximum", label="maximum"
                                ),
                            ],
                        ),
                        (
                            f"{role.name} memory (%)",
                            [
                                service.metric_memory_utilization(label="average"),
                                service.metric_memory_utilization(
                                    statistic="Maximum", label="maximum"
                                ),
                            ],
                        ),
                    ],
                    height=5,
                    left_y_axis=cloudwatch.YAxisProps(min=0, max=100),
                )
            )
        self.add(ROLES, "n8n roles", rows, covers=services)
        main = svc.services["main"]
        self.alarm(
            "MainCpu",
            main.metric_cpu_utilization(period=cdk.Duration.minutes(5)),
            85,
            "n8n main CPU above 85% for 15 minutes",
        )
        if d.finops.schedule.kind != "office-hours":
            # ECS publishes service CPU every minute while a task runs: no sample
            # means no running main task.
            self.alarm(
                "MainDown",
                main.metric_cpu_utilization(
                    statistic="SampleCount", period=cdk.Duration.minutes(5)
                ),
                1,
                "no n8n main task has been running for 10 minutes",
                evaluation_periods=2,
                comparison=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
                missing=cloudwatch.TreatMissingData.BREACHING,
            )

    def _runners(self) -> None:
        d, svc = self.d, self.service
        if d.n8n.runners.mode != "external":
            return
        insights = (
            d.observability.container_insights
            and d.runtime.ecs.cluster.kind == "create"
        )
        if not insights:
            self.add(
                RUNNERS,
                "Task runners",
                [
                    [
                        _text(
                            "Runner sidecar CPU and memory need Container Insights: "
                            "set `observability.container_insights: true`.",
                            height=1,
                        )
                    ]
                ],
            )
            return

        def container(service_name: str, name: str, label: str) -> cloudwatch.Metric:
            return cloudwatch.Metric(
                namespace="ECS/ContainerInsights",
                metric_name=name,
                dimensions_map={
                    "ClusterName": svc.cluster.cluster_name,
                    "ServiceName": service_name,
                    "ContainerName": "runners",
                },
                statistic="Average",
                label=label,
            )

        cpu, memory = [], []
        for role in self.plan.roles:
            name = svc.services[role.name].service_name
            cpu.append(container(name, "ContainerCpuUtilized", role.name))
            memory.append(container(name, "ContainerMemoryUtilized", role.name))
        self.add(
            RUNNERS,
            "Task runners",
            [
                self.graphs(
                    [
                        ("Runners CPU (units)", cpu),
                        ("Runners memory (MiB)", memory),
                    ],
                    height=5,
                )
            ],
        )

    def _logs(self) -> None:
        svc = self.service
        group = svc.logs
        errors = r"filter @message like /(?i)(error|exception|failed)/"
        self.add(
            LOGS,
            "Logs",
            [
                [
                    cloudwatch.LogQueryWidget(
                        title="Errors per role and container",
                        log_group_names=[group.log_group_name],
                        query_lines=[
                            'parse @logStream "*/*/*" as role, container, task',
                            errors,
                            "stats count(*) as errors by bin(5m), role",
                        ],
                        view=cloudwatch.LogQueryVisualizationType.LINE,
                        width=8,
                        height=6,
                    ),
                    cloudwatch.LogQueryWidget(
                        title="Latest errors",
                        log_group_names=[group.log_group_name],
                        query_lines=[
                            "fields @timestamp, @logStream, @message",
                            errors,
                            "sort @timestamp desc",
                            "limit 50",
                        ],
                        view=cloudwatch.LogQueryVisualizationType.TABLE,
                        width=16,
                        height=6,
                    ),
                ]
            ],
            covers=[group],
        )

    def _database(self) -> None:
        d = self.d
        spec = d.database
        if spec.kind == "sqlite":
            return
        if spec.kind == "external":
            self.add(
                DATABASE,
                "Database",
                [[_text(f"PostgreSQL at `{spec.host}` {_NOT_MONITORED}", height=1)]],
            )
            return
        db = self.data.db_construct
        if isinstance(db, rds.DatabaseCluster):
            first: list[tuple[str, Sequence[cloudwatch.IMetric]]] = []
            if spec.kind == "aurora-serverless":
                first.append(
                    (
                        "Capacity (ACU)",
                        [db.metric_serverless_database_capacity(label="ACU used")],
                    )
                )
            first += [
                ("CPU (%)", [db.metric_cpu_utilization(label="CPU")]),
                (
                    "Connections",
                    [db.metric_database_connections(label="connections")],
                ),
                (
                    "Freeable memory (bytes)",
                    [db.metric_freeable_memory(label="freeable")],
                ),
            ]
            rows = [
                self.graphs(first),
                self.graphs(
                    [
                        (
                            "Latency (s)",
                            [
                                db.metric("ReadLatency", label="read"),
                                db.metric("WriteLatency", label="write"),
                            ],
                        ),
                        ("Deadlocks", [db.metric_deadlocks(label="deadlocks")]),
                        (
                            "Volume used (bytes)",
                            [db.metric_volume_bytes_used(label="volume")],
                        ),
                    ]
                ),
            ]
            if spec.kind == "aurora-serverless":
                self.alarm(
                    "DatabaseAtMax",
                    db.metric_acu_utilization(period=cdk.Duration.minutes(5)),
                    90,
                    "Aurora has used more than 90% of max_acu for 15 minutes",
                )
            else:
                self.alarm(
                    "DatabaseCpu",
                    db.metric_cpu_utilization(period=cdk.Duration.minutes(5)),
                    85,
                    "database CPU above 85% for 15 minutes",
                )
            self.add(DATABASE, "Database: Aurora PostgreSQL", rows, covers=[db])
            return
        assert isinstance(db, rds.DatabaseInstance) and spec.kind == "rds"
        rows = [
            self.graphs(
                [
                    ("CPU (%)", [db.metric_cpu_utilization(label="CPU")]),
                    (
                        "Connections",
                        [db.metric_database_connections(label="connections")],
                    ),
                    (
                        "Free storage (bytes)",
                        [db.metric_free_storage_space(label="free")],
                    ),
                    (
                        "IOPS",
                        [
                            db.metric_read_iops(label="read"),
                            db.metric_write_iops(label="write"),
                        ],
                    ),
                ]
            )
        ]
        self.alarm(
            "DatabaseCpu",
            db.metric_cpu_utilization(period=cdk.Duration.minutes(5)),
            85,
            "database CPU above 85% for 15 minutes",
        )
        self.alarm(
            "DatabaseStorage",
            db.metric_free_storage_space(period=cdk.Duration.minutes(5)),
            spec.storage_gb * 1024**3 * 0.1,
            "less than 10% of the database storage is free",
            evaluation_periods=1,
            comparison=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        )
        self.add(DATABASE, "Database: RDS PostgreSQL", rows, covers=[db])

    def _queue(self) -> None:
        d = self.d
        spec = d.queue
        if not d.is_queued or spec.kind == "none":
            return
        if spec.kind == "external":
            self.add(
                QUEUE,
                "Queue",
                [[_text(f"Redis at `{spec.host}` {_NOT_MONITORED}", height=1)]],
            )
            return
        queue = self.data.queue_construct
        assert queue is not None

        def cache(
            name: str, dims: dict[str, str], statistic: str, label: str
        ) -> cloudwatch.Metric:
            return cloudwatch.Metric(
                namespace="AWS/ElastiCache",
                metric_name=name,
                dimensions_map=dims,
                statistic=statistic,
                label=label,
            )

        if spec.kind == "elasticache-serverless":
            dims = {"clusterId": self.data.queue_name}
            self.add(
                QUEUE,
                "Queue: ElastiCache Serverless",
                [
                    self.graphs(
                        [
                            (
                                "ECPUs",
                                [
                                    cache(
                                        "ElastiCacheProcessingUnits",
                                        dims,
                                        "Sum",
                                        "ECPUs",
                                    )
                                ],
                            ),
                            (
                                "Data stored (bytes)",
                                [cache("BytesUsedForCache", dims, "Average", "bytes")],
                            ),
                            (
                                "Connections",
                                [cache("CurrConnections", dims, "Average", "clients")],
                            ),
                            (
                                "Throttled commands",
                                [cache("ThrottledCmds", dims, "Sum", "throttled")],
                            ),
                        ]
                    )
                ],
                covers=[queue],
            )
            self.alarm(
                "QueueThrottled",
                cache("ThrottledCmds", dims, "Sum", "throttled").with_(
                    period=cdk.Duration.minutes(5)
                ),
                0,
                "the queue has throttled commands for 15 minutes",
            )
            return
        assert spec.kind == "elasticache"
        nodes = [
            f"{self.data.queue_name}-{index:03d}"
            for index in range(1, spec.replicas + 2)
        ]

        def per_node(name: str, statistic: str = "Average") -> list[cloudwatch.IMetric]:
            return [
                cache(name, {"CacheClusterId": node}, statistic, node) for node in nodes
            ]

        self.add(
            QUEUE,
            "Queue: ElastiCache",
            [
                self.graphs(
                    [
                        ("Engine CPU (%)", per_node("EngineCPUUtilization")),
                        ("Memory used (%)", per_node("DatabaseMemoryUsagePercentage")),
                        ("Connections", per_node("CurrConnections")),
                        ("Evictions", per_node("Evictions", "Sum")),
                    ]
                )
            ],
            covers=[queue],
        )
        memory = cloudwatch.MathExpression(
            expression="MAX([" + ",".join(f"m{i}" for i in range(len(nodes))) + "])",
            using_metrics={
                f"m{i}": metric
                for i, metric in enumerate(per_node("DatabaseMemoryUsagePercentage"))
            },
            period=cdk.Duration.minutes(5),
            label="memory used (%)",
        )
        self.alarm("QueueMemory", memory, 80, "queue memory above 80% for 15 minutes")

    def _files(self) -> None:
        fs = self.data.file_system
        if fs is None:
            return
        dims = {"FileSystemId": fs.file_system_id}

        def efs(
            name: str, statistic: str, label: str, **extra: str
        ) -> cloudwatch.Metric:
            return cloudwatch.Metric(
                namespace="AWS/EFS",
                metric_name=name,
                dimensions_map={**dims, **extra},
                statistic=statistic,
                label=label,
            )

        self.add(
            FILES,
            "File system: EFS",
            [
                self.graphs(
                    [
                        (
                            "I/O limit used (%)",
                            [efs("PercentIOLimit", "Maximum", "I/O limit")],
                        ),
                        (
                            "Throughput (bytes)",
                            [
                                efs("DataReadIOBytes", "Sum", "read"),
                                efs("DataWriteIOBytes", "Sum", "write"),
                            ],
                        ),
                        (
                            "Client connections",
                            [efs("ClientConnections", "Sum", "clients")],
                        ),
                        (
                            "Storage (bytes)",
                            [
                                efs(
                                    "StorageBytes",
                                    "Average",
                                    "total",
                                    StorageClass="Total",
                                )
                            ],
                        ),
                    ]
                )
            ],
            covers=[fs],
        )
        self.alarm(
            "FilesIoLimit",
            efs("PercentIOLimit", "Maximum", "I/O limit").with_(
                period=cdk.Duration.minutes(5)
            ),
            90,
            "EFS is above 90% of its I/O limit for 15 minutes",
        )

    def _storage(self) -> None:
        spec = self.d.storage.bucket
        if spec.kind == "none":
            return
        bucket = self.data.bucket
        assert bucket is not None
        name = spec.name if spec.kind == "existing" else bucket.bucket_name

        def s3(name_: str, storage: str, label: str) -> cloudwatch.Metric:
            return cloudwatch.Metric(
                namespace="AWS/S3",
                metric_name=name_,
                dimensions_map={"BucketName": name, "StorageType": storage},
                statistic="Average",
                label=label,
                period=cdk.Duration.days(1),
            )

        self.add(
            STORAGE,
            "Storage: S3",
            [
                self.graphs(
                    [
                        (
                            "Bucket size (bytes, daily)",
                            [s3("BucketSizeBytes", "StandardStorage", "size")],
                        ),
                        (
                            "Objects (daily)",
                            [s3("NumberOfObjects", "AllStorageTypes", "objects")],
                        ),
                    ],
                    height=5,
                    period=cdk.Duration.days(1),
                    start="-P14D",
                )
            ],
            covers=[bucket] if spec.kind == "create" else [],
        )

    def _network(self) -> None:
        vpc_spec = self.d.network.vpc
        if vpc_spec.kind == "existing":
            self.add(
                NETWORK,
                "Network",
                [[_text(f"The NAT of VPC `{vpc_spec.id}` {_NOT_MONITORED}", height=1)]],
            )
            return
        vpc = self.network.vpc
        gateways = [c for c in vpc.node.find_all() if isinstance(c, ec2.CfnNatGateway)]
        instances = [c for c in vpc.node.find_all() if isinstance(c, ec2.CfnInstance)]
        if gateways:

            def nat(name: str, statistic: str = "Sum") -> list[cloudwatch.Metric]:
                return [
                    cloudwatch.Metric(
                        namespace="AWS/NATGateway",
                        metric_name=name,
                        dimensions_map={"NatGatewayId": gateway.ref},
                        statistic=statistic,
                        label=f"gateway {index + 1}",
                    )
                    for index, gateway in enumerate(gateways)
                ]

            self.add(
                NETWORK,
                "Network: NAT gateway",
                [
                    self.graphs(
                        [
                            ("Bytes out to the internet", nat("BytesOutToDestination")),
                            (
                                "Active connections",
                                nat("ActiveConnectionCount", "Maximum"),
                            ),
                            ("Port allocation errors", nat("ErrorPortAllocation")),
                            ("Dropped packets", nat("PacketsDropCount")),
                        ]
                    )
                ],
                covers=gateways,
            )
            for index, metric in enumerate(nat("ErrorPortAllocation")):
                self.alarm(
                    f"NatPorts{index + 1}",
                    metric.with_(period=cdk.Duration.minutes(5)),
                    0,
                    "the NAT gateway ran out of ports for 15 minutes",
                )
        if instances:

            def ec2_(name: str, statistic: str) -> list[cloudwatch.Metric]:
                return [
                    cloudwatch.Metric(
                        namespace="AWS/EC2",
                        metric_name=name,
                        dimensions_map={"InstanceId": instance.ref},
                        statistic=statistic,
                        label=f"NAT instance {index + 1}",
                    )
                    for index, instance in enumerate(instances)
                ]

            self.add(
                NETWORK,
                "Network: NAT instance",
                [
                    self.graphs(
                        [
                            ("CPU (%)", ec2_("CPUUtilization", "Average")),
                            ("Bytes out", ec2_("NetworkOut", "Sum")),
                            (
                                "Status check failed",
                                ec2_("StatusCheckFailed", "Maximum"),
                            ),
                        ]
                    )
                ],
                covers=instances,
            )
            for index, metric in enumerate(ec2_("StatusCheckFailed", "Maximum")):
                self.alarm(
                    f"NatInstance{index + 1}",
                    metric.with_(period=cdk.Duration.minutes(5)),
                    0,
                    "the NAT instance fails its status checks: tasks lose egress",
                    evaluation_periods=2,
                )

    def _glue(self) -> None:
        function = self.service.origin_function
        if function is None:
            return
        self.add(
            GLUE,
            "Origin updater",
            [
                self.graphs(
                    [
                        (
                            "Invocations and errors",
                            [
                                function.metric_invocations(label="invocations"),
                                function.metric_errors(label="errors"),
                            ],
                        ),
                        ("Duration (ms)", [function.metric_duration(label="duration")]),
                    ],
                    height=5,
                )
            ],
            covers=[function],
        )
        self.alarm(
            "OriginUpdaterErrors",
            function.metric_errors(period=cdk.Duration.minutes(5)),
            0,
            "the CloudFront origin may point at a stopped task",
            evaluation_periods=1,
        )

    def _cost(self) -> None:
        d = self.d
        tag = f"n8n-on-aws:deployment = {d.name}"
        parts = [
            "[Cost Explorer](https://us-east-1.console.aws.amazon.com/costmanagement/"
            f"home#/cost-explorer): filter on the cost allocation tag `{tag}`"
        ]
        if d.finops.budget.monthly_usd is not None:
            parts.append(
                f"[Budget `{d.name}-monthly`](https://us-east-1.console.aws.amazon.com"
                f"/billing/home#/budgets): {d.finops.budget.monthly_usd:g} USD a month"
            )
        if d.finops.anomaly_detection:
            parts.append(
                "[Cost anomalies](https://us-east-1.console.aws.amazon.com/"
                "costmanagement/home#/anomaly-detection/overview)"
            )
        self.add(COST, "Cost", [[_text(" · ".join(parts), height=1)]])


def _kebab(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()


def _resources(app: cdk.App) -> Iterator[cdk.CfnResource]:
    for stack in app.node.children:
        if isinstance(stack, cdk.Stack):
            for construct in stack.node.find_all():
                if isinstance(construct, cdk.CfnResource):
                    yield construct


def unmonitored(app: cdk.App, builder: DashboardBuilder) -> list[str]:
    """Paths of monitored-type resources that no dashboard section watches."""
    missing: list[str] = []
    for resource in _resources(app):
        if resource.cfn_resource_type not in MONITORED_TYPES:
            continue
        path = resource.node.path
        if any(marker in path for marker in FRAMEWORK_MARKERS):
            continue
        if not any(path == c or path.startswith(c + "/") for c in builder.covered):
            missing.append(f"{resource.cfn_resource_type} {path}")
    return missing
