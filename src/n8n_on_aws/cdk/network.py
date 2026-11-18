"""The ``<name>-network`` stack: VPC, subnets, endpoints and security groups."""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_logs as logs
from aws_cdk import custom_resources as cr
from constructs import Construct

from ..config.models import Deployment

N8N_PORT = 5678

_INTERFACE_ENDPOINTS: dict[str, list[ec2.InterfaceVpcEndpointAwsService]] = {
    "ecr": [
        ec2.InterfaceVpcEndpointAwsService.ECR,
        ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
    ],
    "logs": [ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS],
    "secretsmanager": [ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER],
    "sts": [ec2.InterfaceVpcEndpointAwsService.STS],
}


class NetworkStack(cdk.Stack):
    """Where n8n runs: subnets for ingress, tasks and data, and their firewalls."""

    vpc: ec2.IVpc
    task_subnets: ec2.SubnetSelection
    ingress_subnets: ec2.SubnetSelection
    data_subnets: ec2.SubnetSelection
    public_ips: bool
    sg_ingress: ec2.SecurityGroup | None
    sg_tasks: ec2.SecurityGroup
    sg_data: ec2.SecurityGroup

    def __init__(
        self, scope: Construct, construct_id: str, d: Deployment, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.d = d
        if d.network.vpc.kind == "create":
            self._create_vpc()
        else:
            self._import_vpc()
        self._security_groups()

    # -------------------------------------------------------------- vpc

    def _create_vpc(self) -> None:
        spec = self.d.network.vpc
        assert spec.kind == "create"
        subnets = [
            ec2.SubnetConfiguration(
                name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
            )
        ]
        if spec.nat != "none":
            subnets.append(
                ec2.SubnetConfiguration(
                    name="app",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=22,
                )
            )
        subnets.append(
            ec2.SubnetConfiguration(
                name="data", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24
            )
        )
        nat_gateways = {"none": 0, "instance": 1, "single": 1, "per-az": spec.azs}
        provider = None
        if spec.nat == "instance":
            provider = ec2.NatProvider.instance_v2(
                instance_type=ec2.InstanceType("t4g.nano"),
                machine_image=ec2.MachineImage.latest_amazon_linux2023(
                    cpu_type=ec2.AmazonLinuxCpuType.ARM_64
                ),
                default_allowed_traffic=ec2.NatTrafficDirection.OUTBOUND_ONLY,
            )
        vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr(spec.cidr),
            max_azs=spec.azs,
            subnet_configuration=subnets,
            nat_gateways=nat_gateways[spec.nat],
            nat_gateway_provider=provider,
            restrict_default_security_group=True,
        )
        self.vpc = vpc
        self.public_ips = spec.nat == "none"
        public = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC)
        private = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
        self.task_subnets = public if self.public_ips else private
        self.ingress_subnets = public
        self.data_subnets = ec2.SubnetSelection(
            subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
        )
        vpc.add_gateway_endpoint("S3", service=ec2.GatewayVpcEndpointAwsService.S3)
        for name in spec.endpoints:
            for index, service in enumerate(_INTERFACE_ENDPOINTS.get(name, [])):
                vpc.add_interface_endpoint(
                    f"{name.capitalize()}{index or ''}Endpoint",
                    service=service,
                    subnets=self.task_subnets,
                )

    def _import_vpc(self) -> None:
        spec = self.d.network.vpc
        assert spec.kind == "existing"
        self.vpc = ec2.Vpc.from_vpc_attributes(
            self,
            "Vpc",
            vpc_id=spec.id,
            availability_zones=spec.availability_zones,
            private_subnet_ids=spec.private_subnets,
            public_subnet_ids=spec.public_subnets or None,
        )
        private = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
        self.public_ips = False
        self.task_subnets = private
        self.data_subnets = private
        self.ingress_subnets = (
            ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC)
            if spec.public_subnets
            else private
        )

    # -------------------------------------------------------- firewalls

    def cloudfront_prefix_list(self) -> str:
        """ID of the managed prefix list of CloudFront origin-facing addresses."""
        lookup = cr.AwsCustomResource(
            self,
            "CloudFrontPrefixList",
            on_update=cr.AwsSdkCall(
                service="EC2",
                action="describeManagedPrefixLists",
                parameters={
                    "Filters": [
                        {
                            "Name": "prefix-list-name",
                            "Values": ["com.amazonaws.global.cloudfront.origin-facing"],
                        }
                    ]
                },
                physical_resource_id=cr.PhysicalResourceId.of("cloudfront-prefix-list"),
                output_paths=["PrefixLists.0.PrefixListId"],
            ),
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE
            ),
            install_latest_aws_sdk=False,
            log_group=logs.LogGroup(
                self,
                "CloudFrontPrefixListLogs",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        return lookup.get_response_field("PrefixLists.0.PrefixListId")

    def _security_groups(self) -> None:
        d = self.d
        self.sg_tasks = ec2.SecurityGroup(
            self, "Tasks", vpc=self.vpc, description="n8n tasks"
        )
        self.sg_data = ec2.SecurityGroup(
            self,
            "Data",
            vpc=self.vpc,
            description="n8n data services",
            allow_all_outbound=False,
        )
        self.sg_ingress = None
        cloudfront = d.ingress.kind == "cloudfront"
        if d.has_alb:
            self.sg_ingress = ec2.SecurityGroup(
                self, "Ingress", vpc=self.vpc, description="n8n load balancer"
            )
            self.sg_tasks.add_ingress_rule(
                self.sg_ingress, ec2.Port.tcp(N8N_PORT), "from the load balancer"
            )
            if cloudfront:
                self.sg_ingress.add_ingress_rule(
                    ec2.Peer.prefix_list(self.cloudfront_prefix_list()),
                    ec2.Port.tcp(80),
                    "from CloudFront",
                )
            else:
                ports = [443, 80] if d.ingress.domain else [80]
                for cidr in d.ingress.allowed_cidrs:
                    peer = ec2.Peer.ipv4(cidr)
                    for port in ports:
                        self.sg_ingress.add_ingress_rule(
                            peer, ec2.Port.tcp(port), f"from {cidr}"
                        )
        elif cloudfront:
            self.sg_tasks.add_ingress_rule(
                ec2.Peer.prefix_list(self.cloudfront_prefix_list()),
                ec2.Port.tcp(N8N_PORT),
                "from CloudFront",
            )
        for port, label in ((5432, "PostgreSQL"), (6379, "Redis"), (2049, "EFS")):
            self.sg_data.add_ingress_rule(
                self.sg_tasks, ec2.Port.tcp(port), f"{label} from n8n"
            )
