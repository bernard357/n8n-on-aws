"""n8n's own Prometheus metrics, published to CloudWatch by an agent sidecar.

n8n serves ``/metrics`` on its main port, without authentication. A CloudWatch agent
in the main task scrapes it over localhost and writes the selected metrics as
embedded metric format (EMF) logs, which CloudWatch turns into metrics of the
``n8n-on-aws`` namespace with a ``Deployment`` dimension. The queue gauges exist only
on main instances in queue mode; they drive worker scaling on queue depth.
"""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cloudwatch

from ..config.models import Deployment

AGENT_IMAGE = "public.ecr.aws/cloudwatch-agent/cloudwatch-agent:1.300074.0b1920"
NAMESPACE = "n8n-on-aws"
METRICS_PATH = "/metrics"
SCRAPE_SECONDS = 30

QUEUE_METRICS = (
    "n8n_scaling_mode_queue_jobs_waiting",
    "n8n_scaling_mode_queue_jobs_active",
    "n8n_scaling_mode_queue_jobs_completed",
    "n8n_scaling_mode_queue_jobs_failed",
)

# A short list on purpose: each published metric costs $0.30 a month.
PROCESS_METRICS = (
    "n8n_active_workflow_count",
    "n8n_instance_role_leader",
    "n8n_nodejs_eventloop_lag_p99_seconds",
    "n8n_nodejs_heap_size_used_bytes",
    "n8n_process_resident_memory_bytes",
)


def selected(d: Deployment) -> list[str]:
    """The metric names the agent publishes for this deployment."""
    names: list[str] = []
    if d.is_queued:
        names.extend(QUEUE_METRICS)
    if d.observability.prometheus == "cloudwatch":
        names.extend(PROCESS_METRICS)
    return names


def log_group_name(d: Deployment) -> str:
    return f"/n8n-on-aws/{d.name}/metrics"


def agent_environment(d: Deployment) -> dict[str, str]:
    """``CW_CONFIG_CONTENT`` and ``PROMETHEUS_CONFIG_CONTENT`` for the sidecar."""
    prometheus = {
        "global": {
            "scrape_interval": f"{SCRAPE_SECONDS}s",
            "scrape_timeout": "10s",
        },
        "scrape_configs": [
            {
                "job_name": "n8n",
                "metrics_path": METRICS_PATH,
                "static_configs": [
                    {
                        "targets": ["localhost:5678"],
                        "labels": {"Deployment": d.name},
                    }
                ],
            }
        ],
    }
    selectors = [f"^{name}$" for name in selected(d)]
    agent = {
        "logs": {
            "metrics_collected": {
                "prometheus": {
                    "log_group_name": log_group_name(d),
                    "prometheus_config_path": "env:PROMETHEUS_CONFIG_CONTENT",
                    "emf_processor": {
                        "metric_declaration_dedup": True,
                        "metric_namespace": NAMESPACE,
                        "metric_declaration": [
                            {
                                "source_labels": ["job"],
                                "label_matcher": "^n8n$",
                                "dimensions": [["Deployment"]],
                                "metric_selectors": selectors,
                            }
                        ],
                    },
                }
            },
            "force_flush_interval": 5,
        },
    }
    return {
        "CW_CONFIG_CONTENT": json.dumps(agent, separators=(",", ":")),
        # YAML is a superset of JSON, so the agent reads this as is.
        "PROMETHEUS_CONFIG_CONTENT": json.dumps(prometheus, separators=(",", ":")),
    }


def metric(
    d: Deployment,
    name: str,
    *,
    statistic: str = "Maximum",
    label: str | None = None,
    period: cdk.Duration | None = None,
) -> cloudwatch.Metric:
    """One of n8n's metrics as published by the agent."""
    return cloudwatch.Metric(
        namespace=NAMESPACE,
        metric_name=name,
        dimensions_map={"Deployment": d.name},
        statistic=statistic,
        label=label or name.removeprefix("n8n_"),
        period=period or cdk.Duration.minutes(1),
    )
