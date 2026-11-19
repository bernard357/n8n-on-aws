"""What an EC2 instance type offers to ECS tasks, without calling AWS.

Sizes follow the naming of current Nitro families (``m7g.large``: 2 vCPU,
8 GiB). The network interface limits are those of the EC2 documentation for
general purpose, compute and memory families; ``t`` types have their own table.
Unknown names give ``None`` and the checks that need them are skipped.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from .models import Deployment

# ECS keeps part of the memory for the OS and its agent.
ECS_MEMORY_SHARE = 0.9

_VCPU = {
    "medium": 1,
    "large": 2,
    "xlarge": 4,
    "2xlarge": 8,
    "4xlarge": 16,
    "8xlarge": 32,
    "12xlarge": 48,
    "16xlarge": 64,
}
_ENIS = {
    "medium": 2,
    "large": 3,
    "xlarge": 4,
    "2xlarge": 4,
    "4xlarge": 8,
    "8xlarge": 8,
    "12xlarge": 8,
    "16xlarge": 15,
}
_GIB_PER_VCPU = {"c": 2, "m": 4, "r": 8}
# t types: (vCPU, GiB, network interfaces)
_BURSTABLE = {
    "nano": (2, 0.5, 2),
    "micro": (2, 1, 2),
    "small": (2, 2, 3),
    "medium": (2, 4, 3),
    "large": (2, 8, 3),
    "xlarge": (4, 16, 4),
    "2xlarge": (8, 32, 4),
}
_NAME = re.compile(
    r"^(?P<family>(?P<cls>[a-z]+?)\d+(?P<extra>[a-z-]*))\.(?P<size>\w+)$"
)


class InstanceSpec(BaseModel):
    name: str
    vcpu: int
    memory_gib: float
    enis: int
    arm: bool

    @property
    def cpu_units(self) -> int:
        return self.vcpu * 1024

    @property
    def task_memory_mib(self) -> int:
        """Memory ECS can give to tasks."""
        return int(self.memory_gib * 1024 * ECS_MEMORY_SHARE)

    @property
    def task_slots(self) -> int:
        """awsvpc tasks per instance without ENI trunking (one ENI is the host's)."""
        return self.enis - 1


def is_arm(name: str) -> bool:
    """Graviton families have a ``g`` after the generation: m7g, c7gn, t4g."""
    match = _NAME.match(name)
    return bool(match and match.group("extra").startswith("g"))


def spec(name: str) -> InstanceSpec | None:
    match = _NAME.match(name)
    if match is None:
        return None
    cls, size = match.group("cls"), match.group("size")
    arm = is_arm(name)
    if cls == "t":
        if size not in _BURSTABLE:
            return None
        vcpu, memory, enis = _BURSTABLE[size]
        return InstanceSpec(name=name, vcpu=vcpu, memory_gib=memory, enis=enis, arm=arm)
    if cls not in _GIB_PER_VCPU or size not in _VCPU:
        return None
    vcpu = _VCPU[size]
    return InstanceSpec(
        name=name,
        vcpu=vcpu,
        memory_gib=vcpu * _GIB_PER_VCPU[cls],
        enis=_ENIS[size],
        arm=arm,
    )


class Tasks(BaseModel):
    """Tasks of one role on one group of instances."""

    path: str
    cpu: int
    memory: int
    low: int
    high: int


def placement(d: Deployment) -> dict[str, list[Tasks]]:
    """Tasks of each role per Auto Scaling group: ``on-demand`` and ``spot``.

    With ``topology.workers.spot``, the first worker stays on-demand and the
    others run on Spot, like the Fargate strategy.
    """
    t = d.topology
    groups: dict[str, list[Tasks]] = {"on-demand": [], "spot": []}
    groups["on-demand"].append(
        Tasks(
            path="topology.main",
            cpu=t.main.cpu,
            memory=t.main.memory,
            low=t.main.replicas,
            high=t.main.replicas,
        )
    )
    if t.mode in ("split", "multi-main"):
        groups["on-demand"].append(
            Tasks(
                path="topology.webhook",
                cpu=t.webhook.cpu,
                memory=t.webhook.memory,
                low=t.webhook.min,
                high=t.webhook.max,
            )
        )
    if d.is_queued:
        w = t.workers
        base = 1 if w.spot and w.min else 0
        on_demand = (base, base) if w.spot else (w.min, w.max)
        groups["on-demand"].append(
            Tasks(
                path="topology.workers",
                cpu=w.cpu,
                memory=w.memory,
                low=on_demand[0],
                high=on_demand[1],
            )
        )
        if w.spot:
            groups["spot"].append(
                Tasks(
                    path="topology.workers",
                    cpu=w.cpu,
                    memory=w.memory,
                    low=w.min - base,
                    high=w.max - base,
                )
            )
    return {name: [x for x in tasks if x.high] for name, tasks in groups.items()}


def instances_for(tasks: list[Tasks], instance: InstanceSpec, high: bool) -> int:
    """Instances that hold ``tasks`` at their low or high count."""
    count = sum(x.high if high else x.low for x in tasks)
    if not count:
        return 0
    cpu = sum(x.cpu * (x.high if high else x.low) for x in tasks)
    memory = sum(x.memory * (x.high if high else x.low) for x in tasks)
    return max(
        -(-cpu // instance.cpu_units),
        -(-memory // instance.task_memory_mib),
        -(-count // instance.task_slots),
    )
