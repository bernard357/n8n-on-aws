"""The ``<name>-n8n`` stack: ECS services, ingress, login wall, operations."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

import aws_cdk as cdk
from aws_cdk import aws_applicationautoscaling as appscaling
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_elasticloadbalancingv2_actions as elb_actions
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_wafv2 as wafv2
from aws_cdk import custom_resources as cr
from constructs import Construct

if TYPE_CHECKING:
    from .dashboard import DashboardBuilder

from ..config.models import Deployment, SecretRef
from ..plan import PUBLIC_PATHS, Plan, Role
from . import origin_updater
from .data import DataStack
from .edge import EdgeStack, hosted_zone
from .helpers import chunks, import_secret, retention
from .network import N8N_PORT, NetworkStack

WEBHOOK_PATHS = ["/webhook/*", "/webhook-waiting/*", "/form/*", "/form-waiting/*"]
TASK_ORIGIN_ID = "n8n-task"
TASK_ORIGIN_PLACEHOLDER = "example.com"
RDS_CA_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
INIT_IMAGE = "public.ecr.aws/amazonlinux/amazonlinux:2023"

_PRICE_CLASS = {
    "100": cloudfront.PriceClass.PRICE_CLASS_100,
    "200": cloudfront.PriceClass.PRICE_CLASS_200,
    "all": cloudfront.PriceClass.PRICE_CLASS_ALL,
}


class ServiceStack(cdk.Stack):
    """n8n itself: one ECS service per role, and the way users reach it."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        d: Deployment,
        plan: Plan,
        network: NetworkStack,
        data: DataStack,
        edge: EdgeStack | None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.d = d
        self.plan = plan
        self.network = network
        self.data = data
        self.edge = edge
        self.services: dict[str, ecs.FargateService] = {}
        self.target_groups: dict[str, elbv2.ApplicationTargetGroup] = {}
        self.alb: elbv2.ApplicationLoadBalancer | None = None
        self.distribution: cloudfront.Distribution | None = None
        self.origin_function: lambda_.Function | None = None
        self.operations: DashboardBuilder | None = None
        self.zone = hosted_zone(self, d) if d.ingress.domain else None
        self._imported: dict[str, secretsmanager.ISecret] = {}
        self._import_data()
        self.url = ""  # resolved once the ingress exists

        self._cluster()
        if d.has_alb:
            self._load_balancer()
        if d.ingress.kind == "cloudfront":
            self._distribution()
        self.url = self._resolve(plan.url)
        for role in plan.roles:
            self._service(role)
        if self.alb is not None:
            self._listener()
            self._alb_waf()
        self._dns()
        if d.ingress.kind == "cloudfront" and d.ingress.cloudfront.origin == "task":
            self._origin_updater()
        self._budget()
        self._outputs()

    # --------------------------------------------------------------- cluster

    def _cluster(self) -> None:
        d = self.d
        spec = d.runtime.ecs.cluster
        if spec.kind == "existing":
            self.cluster: ecs.ICluster = ecs.Cluster.from_cluster_attributes(
                self, "Cluster", cluster_name=spec.name, vpc=self.network.vpc
            )
        else:
            insights = (
                ecs.ContainerInsights.ENHANCED
                if d.observability.container_insights
                else ecs.ContainerInsights.DISABLED
            )
            self.cluster = ecs.Cluster(
                self,
                "Cluster",
                cluster_name=d.name,
                vpc=self.network.vpc,
                enable_fargate_capacity_providers=True,
                container_insights_v2=insights,
            )
        self.logs = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/n8n-on-aws/{d.name}",
            retention=retention(d.observability.log_retention_days),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        self.task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description=f"n8n tasks of {d.name}",
        )
        if self.bucket is not None:
            self.bucket.grant_read_write(self.task_role)
        if self.key is not None:
            self.key.grant_encrypt_decrypt(self.task_role)
        if self.data.file_system is not None:
            self.data.file_system.grant_read_write(self.task_role)

    # ------------------------------------------------------------- ingress

    def _https_on_alb(self) -> bool:
        return self.d.ingress.kind == "alb" and self.d.ingress.domain is not None

    def _alb_certificate(self) -> acm.ICertificate:
        spec = self.d.ingress.certificate
        if spec.kind == "existing":
            return acm.Certificate.from_certificate_arn(self, "Certificate", spec.arn)
        assert self.d.ingress.domain is not None
        return acm.Certificate(
            self,
            "Certificate",
            domain_name=self.d.ingress.domain,
            validation=acm.CertificateValidation.from_dns(self.zone),
        )

    def _load_balancer(self) -> None:
        d = self.d
        net = self.network
        assert net.sg_ingress is not None
        internet_facing = d.ingress.kind == "alb" and d.ingress.public
        subnets = net.ingress_subnets if internet_facing else net.task_subnets
        if not internet_facing and net.public_ips:
            subnets = net.data_subnets
        self.alb = elbv2.ApplicationLoadBalancer(
            self,
            "LoadBalancer",
            vpc=net.vpc,
            internet_facing=internet_facing,
            vpc_subnets=subnets,
            security_group=net.sg_ingress,
            idle_timeout=cdk.Duration.seconds(d.ingress.idle_timeout_seconds),
            drop_invalid_header_fields=True,
        )
        roles = {role.name for role in self.plan.roles}
        for name in ("main", "webhook"):
            if name not in roles:
                continue
            sticky = name == "main" and d.topology.mode == "multi-main"
            self.target_groups[name] = elbv2.ApplicationTargetGroup(
                self,
                f"{name.capitalize()}Targets",
                vpc=net.vpc,
                port=N8N_PORT,
                protocol=elbv2.ApplicationProtocol.HTTP,
                target_type=elbv2.TargetType.IP,
                deregistration_delay=cdk.Duration.seconds(30),
                health_check=elbv2.HealthCheck(
                    path="/healthz", healthy_http_codes="200"
                ),
                stickiness_cookie_duration=cdk.Duration.hours(1) if sticky else None,
            )

    def _distribution(self) -> None:
        d = self.d
        spec = d.ingress.cloudfront
        timeout = cdk.Duration.seconds(60)
        origin: cloudfront.IOrigin
        if spec.origin == "alb":
            assert self.alb is not None
            origin = origins.VpcOrigin.with_application_load_balancer(
                self.alb,
                http_port=80,
                protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                read_timeout=timeout,
            )
        else:
            origin = origins.HttpOrigin(
                TASK_ORIGIN_PLACEHOLDER,
                origin_id=TASK_ORIGIN_ID,
                http_port=N8N_PORT,
                protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                read_timeout=timeout,
            )
        certificate: acm.ICertificate | None = None
        if d.ingress.domain:
            cert_spec = d.ingress.certificate
            if cert_spec.kind == "existing":
                certificate = acm.Certificate.from_certificate_arn(
                    self, "EdgeCertificate", cert_spec.arn
                )
            elif self.edge is not None:
                certificate = self.edge.certificate
            else:  # this stack is itself in us-east-1
                certificate = acm.Certificate(
                    self,
                    "EdgeCertificate",
                    domain_name=d.ingress.domain,
                    validation=acm.CertificateValidation.from_dns(self.zone),
                )
        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment=f"n8n {d.name}",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
                compress=True,
            ),
            domain_names=[d.ingress.domain] if d.ingress.domain else None,
            certificate=certificate,
            price_class=_PRICE_CLASS[spec.price_class],
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
        )

    # ------------------------------------------------------------- services

    def _placeholders(self) -> dict[str, str]:
        values: dict[str, str] = {"${aws.region}": self.region}
        data = self.data
        if data.db_endpoint:
            values["${database.endpoint}"] = data.db_endpoint
        if data.queue_endpoint:
            values["${queue.endpoint}"] = data.queue_endpoint
        if data.bucket is not None:
            values["${storage.bucket}"] = data.bucket.bucket_name
        if self.distribution is not None:
            values["${cloudfront.domain}"] = self.distribution.distribution_domain_name
        if self.alb is not None:
            values["${alb.dns_name}"] = self.alb.load_balancer_dns_name
        return values

    def _resolve(self, value: str) -> str:
        for placeholder, token in self._placeholders().items():
            value = value.replace(placeholder, token)
        return value

    def _import_data(self) -> None:
        """Re-import data-stack resources so grants never edit that stack.

        Granting on the data stack's own objects would add this stack's roles to
        the KMS key policy, making the data stack depend on this one (a cycle).
        Imported references only change this stack's IAM policies.
        """
        data = self.data
        d = self.d
        self.key = (
            kms.Key.from_key_arn(self, "DataKey", data.kms_key.key_arn)
            if data.kms_key is not None
            else None
        )
        self.bucket: s3.IBucket | None = None
        if data.bucket is not None:
            self.bucket = s3.Bucket.from_bucket_attributes(
                self,
                "DataBucket",
                bucket_arn=data.bucket.bucket_arn,
                encryption_key=self.key,
            )

        def local(
            construct_id: str, secret: secretsmanager.ISecret
        ) -> secretsmanager.ISecret:
            return secretsmanager.Secret.from_secret_attributes(
                self,
                construct_id,
                secret_complete_arn=secret.secret_arn,
                encryption_key=self.key,
            )

        self.generated: dict[str, secretsmanager.ISecret] = {
            f"{d.name}/n8n-runners-token": local("RunnersToken", data.runners_token),
        }
        key_name = f"{d.name}/n8n-encryption-key"
        if d.n8n.encryption_key.kind == "generate":
            self.generated[key_name] = local("EncryptionKey", data.encryption_key)
        if data.db_secret is not None:
            self.generated["${database.secret}"] = local(
                "DatabaseSecret", data.db_secret
            )

    def _secret(self, ref: SecretRef) -> ecs.Secret:
        secret = self.generated.get(ref.secret)
        if secret is None:
            secret = self._imported.get(ref.secret)
        if secret is None:
            digest = hashlib.sha256(ref.secret.encode()).hexdigest()[:8]
            secret = import_secret(self, f"Secret{digest}", ref)
            self._imported[ref.secret] = secret
        if ref.key:
            return ecs.Secret.from_secrets_manager(secret, ref.key)
        return ecs.Secret.from_secrets_manager(secret)

    def _service(self, role: Role) -> None:
        d = self.d
        title = role.name.capitalize()
        architecture = (
            ecs.CpuArchitecture.ARM64
            if d.runtime.ecs.architecture == "arm64"
            else ecs.CpuArchitecture.X86_64
        )
        task = ecs.FargateTaskDefinition(
            self,
            f"{title}Task",
            family=f"{d.name}-{role.name}",
            cpu=role.cpu,
            memory_limit_mib=role.memory,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=architecture,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
            task_role=self.task_role,
        )
        environment = {key: self._resolve(value) for key, value in role.env.items()}
        secrets = {key: self._secret(ref) for key, ref in role.secrets.items()}
        args = role.command.split()[1:]  # the image's entrypoint is n8n itself
        n8n = task.add_container(
            "n8n",
            image=ecs.ContainerImage.from_registry(f"{d.n8n.image}:{d.n8n.version}"),
            command=args if args != ["start"] else None,
            environment=environment,
            secrets=secrets,
            port_mappings=[ecs.PortMapping(container_port=N8N_PORT)],
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix=role.name, log_group=self.logs
            ),
            health_check=ecs.HealthCheck(
                command=[
                    "CMD-SHELL",
                    f"wget -q -O /dev/null http://127.0.0.1:{N8N_PORT}/healthz"
                    " || exit 1",
                ],
                start_period=cdk.Duration.seconds(120),
            ),
            stop_timeout=cdk.Duration.seconds(35),
        )
        if "DB_POSTGRESDB_SSL_CA_FILE" in environment:
            task.add_volume(name="rds-ca")
            init = task.add_container(
                "rds-ca",
                image=ecs.ContainerImage.from_registry(INIT_IMAGE),
                essential=False,
                command=[
                    "sh",
                    "-c",
                    f"curl -fsSL {RDS_CA_URL} -o /certs/global-bundle.pem",
                ],
                logging=ecs.LogDrivers.aws_logs(
                    stream_prefix="init", log_group=self.logs
                ),
            )
            init.add_mount_points(
                ecs.MountPoint(
                    source_volume="rds-ca", container_path="/certs", read_only=False
                )
            )
            n8n.add_mount_points(
                ecs.MountPoint(
                    source_volume="rds-ca",
                    container_path="/etc/ssl/rds",
                    read_only=True,
                )
            )
            n8n.add_container_dependencies(
                ecs.ContainerDependency(
                    container=init, condition=ecs.ContainerDependencyCondition.SUCCESS
                )
            )
        data = self.data
        if data.file_system is not None and data.access_point is not None:
            task.add_volume(
                name="n8n-home",
                efs_volume_configuration=ecs.EfsVolumeConfiguration(
                    file_system_id=data.file_system.file_system_id,
                    transit_encryption="ENABLED",
                    authorization_config=ecs.AuthorizationConfig(
                        access_point_id=data.access_point.access_point_id, iam="ENABLED"
                    ),
                ),
            )
            n8n.add_mount_points(
                ecs.MountPoint(
                    source_volume="n8n-home",
                    container_path="/home/node/.n8n",
                    read_only=False,
                )
            )
        if d.n8n.runners.mode == "external":
            task.add_container(
                "runners",
                image=ecs.ContainerImage.from_registry(
                    f"{d.n8n.runners.image}:{d.n8n.version}"
                ),
                environment={
                    "N8N_RUNNERS_TASK_BROKER_URI": "http://127.0.0.1:5679",
                    "N8N_RUNNERS_AUTO_SHUTDOWN_TIMEOUT": "15",
                    "GENERIC_TIMEZONE": d.n8n.timezone,
                },
                secrets={
                    "N8N_RUNNERS_AUTH_TOKEN": ecs.Secret.from_secrets_manager(
                        self.generated[f"{d.name}/n8n-runners-token"]
                    )
                },
                logging=ecs.LogDrivers.aws_logs(
                    stream_prefix=f"{role.name}-runners", log_group=self.logs
                ),
            )

        spot = d.runtime.ecs.capacity == "fargate-spot"
        if role.name == "worker" and d.topology.workers.spot:
            strategies = [
                ecs.CapacityProviderStrategy(
                    capacity_provider="FARGATE",
                    base=1 if role.replicas_min else 0,
                    weight=0,
                ),
                ecs.CapacityProviderStrategy(
                    capacity_provider="FARGATE_SPOT", weight=1
                ),
            ]
        else:
            strategies = [
                ecs.CapacityProviderStrategy(
                    capacity_provider="FARGATE_SPOT" if spot else "FARGATE", weight=1
                )
            ]
        exclusive_main = role.name == "main" and d.topology.mode != "multi-main"
        service = ecs.FargateService(
            self,
            f"{title}Service",
            cluster=self.cluster,
            task_definition=task,
            service_name=f"{d.name}-{role.name}",
            desired_count=role.replicas_min,
            assign_public_ip=self.network.public_ips,
            vpc_subnets=self.network.task_subnets,
            security_groups=[self.network.sg_tasks],
            capacity_provider_strategies=strategies,
            circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True),
            enable_execute_command=d.runtime.ecs.exec,
            min_healthy_percent=0 if exclusive_main else 100,
            max_healthy_percent=100 if exclusive_main else 200,
            health_check_grace_period=cdk.Duration.seconds(180)
            if role.name in self.target_groups
            else None,
        )
        # Delete services before the cluster's capacity provider association,
        # otherwise CloudFormation fails with "capacity provider is in use".
        if isinstance(self.cluster, ecs.Cluster):
            service.node.add_dependency(self.cluster)
        target_group = self.target_groups.get(role.name)
        if target_group is not None:
            service.attach_to_application_target_group(target_group)
        self.services[role.name] = service
        self._scaling(role, service)

    def _scaling(self, role: Role, service: ecs.FargateService) -> None:
        d = self.d
        office_hours = d.finops.schedule.kind == "office-hours"
        if role.replicas_min == role.replicas_max and not office_hours:
            return
        scaling = service.auto_scale_task_count(
            min_capacity=role.replicas_min, max_capacity=role.replicas_max
        )
        if role.name == "webhook":
            scaling.scale_on_cpu_utilization(
                "Cpu", target_utilization_percent=d.topology.webhook.scale_on_cpu
            )
        elif role.name == "worker":
            workers = d.topology.workers
            target = workers.target if workers.scale_on == "cpu" else 70
            scaling.scale_on_cpu_utilization("Cpu", target_utilization_percent=target)
        if office_hours:
            schedule = d.finops.schedule
            zone = cdk.TimeZone.of(schedule.timezone)
            start_hour, start_minute = schedule.start.split(":")
            stop_hour, stop_minute = schedule.stop.split(":")
            scaling.scale_on_schedule(
                "Start",
                schedule=appscaling.Schedule.cron(
                    hour=start_hour, minute=start_minute, week_day=schedule.weekdays
                ),
                min_capacity=role.replicas_min,
                max_capacity=role.replicas_max,
                time_zone=zone,
            )
            scaling.scale_on_schedule(
                "Stop",
                schedule=appscaling.Schedule.cron(
                    hour=stop_hour, minute=stop_minute, week_day=schedule.weekdays
                ),
                min_capacity=0,
                max_capacity=0,
                time_zone=zone,
            )

    # ------------------------------------------------------------ listener

    def _wall(self, forward: elbv2.ListenerAction) -> elbv2.ListenerAction:
        d = self.d
        auth = d.auth
        session = cdk.Duration.hours(auth.session_hours)
        if auth.mode == "alb-cognito":
            pool, client, domain = self._cognito()
            return elb_actions.AuthenticateCognitoAction(
                user_pool=pool,
                user_pool_client=client,
                user_pool_domain=domain,
                next=forward,
                session_timeout=session,
            )
        if auth.mode == "alb-oidc":
            oidc = auth.oidc
            assert oidc is not None
            secret = oidc.client_secret
            return elbv2.ListenerAction.authenticate_oidc(
                issuer=oidc.issuer,
                authorization_endpoint=oidc.authorization_endpoint,
                token_endpoint=oidc.token_endpoint,
                user_info_endpoint=oidc.user_info_endpoint,
                client_id=oidc.client_id,
                client_secret=cdk.SecretValue.secrets_manager(
                    secret.secret, json_field=secret.key
                ),
                scope=oidc.scope,
                next=forward,
                session_timeout=session,
            )
        return forward

    def _listener(self) -> None:
        d = self.d
        alb = self.alb
        assert alb is not None
        main = elbv2.ListenerAction.forward([self.target_groups["main"]])
        if self._https_on_alb():
            listener = alb.add_listener(
                "Https",
                port=443,
                open=False,
                certificates=[
                    elbv2.ListenerCertificate.from_certificate_manager(
                        self._alb_certificate()
                    )
                ],
                ssl_policy=elbv2.SslPolicy.RECOMMENDED_TLS,
                default_action=self._wall(main),
            )
            alb.add_listener(
                "Http",
                port=80,
                open=False,
                default_action=elbv2.ListenerAction.redirect(
                    protocol="HTTPS", port="443", permanent=True
                ),
            )
        else:
            listener = alb.add_listener(
                "Http", port=80, open=False, default_action=self._wall(main)
            )
        priority = 10
        routed: list[str] = []
        webhook = self.target_groups.get("webhook")
        if webhook is not None:
            for index, paths in enumerate(chunks(WEBHOOK_PATHS, 5)):
                elbv2.ApplicationListenerRule(
                    self,
                    f"WebhookPaths{index}",
                    listener=listener,
                    priority=priority,
                    conditions=[elbv2.ListenerCondition.path_patterns(paths)],
                    action=elbv2.ListenerAction.forward([webhook]),
                )
                priority += 1
            routed = WEBHOOK_PATHS
        if d.auth.mode in ("alb-cognito", "alb-oidc"):
            public = [p for p in PUBLIC_PATHS + d.auth.public_paths if p not in routed]
            for index, paths in enumerate(chunks(public, 5)):
                elbv2.ApplicationListenerRule(
                    self,
                    f"PublicPaths{index}",
                    listener=listener,
                    priority=100 + index,
                    conditions=[elbv2.ListenerCondition.path_patterns(paths)],
                    action=main,
                )

    def _cognito(
        self,
    ) -> tuple[cognito.IUserPool, cognito.IUserPoolClient, cognito.IUserPoolDomain]:
        d = self.d
        spec = d.auth.cognito
        if spec.kind == "existing":
            return (
                cognito.UserPool.from_user_pool_id(self, "Users", spec.user_pool_id),
                cognito.UserPoolClient.from_user_pool_client_id(
                    self, "UsersClient", spec.client_id
                ),
                cognito.UserPoolDomain.from_domain_name(
                    self, "UsersDomain", spec.domain
                ),
            )
        mfa = {
            "off": cognito.Mfa.OFF,
            "optional": cognito.Mfa.OPTIONAL,
            "required": cognito.Mfa.REQUIRED,
        }[spec.mfa]
        pool = cognito.UserPool(
            self,
            "Users",
            user_pool_name=f"{d.name}-users",
            self_sign_up_enabled=spec.self_signup,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            mfa=mfa,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True)
            if spec.mfa != "off"
            else None,
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=cdk.RemovalPolicy.DESTROY
            if d.environment == "dev"
            else cdk.RemovalPolicy.RETAIN,
        )
        domain = pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"{d.name}-{cdk.Aws.ACCOUNT_ID}"
            ),
        )
        providers = [cognito.UserPoolClientIdentityProvider.COGNITO]
        federation = spec.federation
        identity_provider: Construct | None = None
        if federation.kind == "oidc":
            identity_provider = cognito.UserPoolIdentityProviderOidc(
                self,
                "Federation",
                user_pool=pool,
                name="corporate",
                client_id=federation.client_id,
                client_secret=cdk.SecretValue.secrets_manager(
                    federation.client_secret.secret,
                    json_field=federation.client_secret.key,
                ).unsafe_unwrap(),
                issuer_url=federation.issuer,
                scopes=["openid", "email"],
                attribute_mapping=cognito.AttributeMapping(
                    email=cognito.ProviderAttribute.other("email")
                ),
            )
            providers.append(cognito.UserPoolClientIdentityProvider.custom("corporate"))
        elif federation.kind == "saml":
            identity_provider = cognito.UserPoolIdentityProviderSaml(
                self,
                "Federation",
                user_pool=pool,
                name="corporate",
                metadata=cognito.UserPoolIdentityProviderSamlMetadata.url(
                    federation.metadata_url
                ),
                attribute_mapping=cognito.AttributeMapping(
                    email=cognito.ProviderAttribute.other(
                        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"
                    )
                ),
            )
            providers.append(cognito.UserPoolClientIdentityProvider.custom("corporate"))
        client = pool.add_client(
            "Alb",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[f"https://{d.ingress.domain}/oauth2/idpresponse"],
            ),
            supported_identity_providers=providers,
        )
        if identity_provider is not None:
            client.node.add_dependency(identity_provider)
        for index, email in enumerate(spec.invite):
            cognito.CfnUserPoolUser(
                self,
                f"Invite{index}",
                user_pool_id=pool.user_pool_id,
                username=email,
                user_attributes=[
                    cognito.CfnUserPoolUser.AttributeTypeProperty(
                        name="email", value=email
                    ),
                    cognito.CfnUserPoolUser.AttributeTypeProperty(
                        name="email_verified", value="true"
                    ),
                ],
                desired_delivery_mediums=["EMAIL"],
            )
        return pool, client, domain

    def _alb_waf(self) -> None:
        waf = self.d.ingress.waf
        assert self.alb is not None
        if waf.kind == "none":
            return
        if waf.kind == "existing":
            arn = waf.arn
        else:

            def managed(name: str, priority: int, **extra: Any) -> Any:
                return wafv2.CfnWebACL.RuleProperty(
                    name=name,
                    priority=priority,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS", name=name, **extra
                        )
                    ),
                    visibility_config=_visibility(name),
                )

            acl = wafv2.CfnWebACL(
                self,
                "Firewall",
                scope="REGIONAL",
                default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
                visibility_config=_visibility(f"{self.d.name}-waf"),
                rules=[
                    wafv2.CfnWebACL.RuleProperty(
                        name="RateLimit",
                        priority=0,
                        action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                        statement=wafv2.CfnWebACL.StatementProperty(
                            rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                                limit=waf.rate_limit, aggregate_key_type="IP"
                            )
                        ),
                        visibility_config=_visibility("RateLimit"),
                    ),
                    # n8n webhooks carry large bodies: count, don't block, on size.
                    managed(
                        "AWSManagedRulesCommonRuleSet",
                        1,
                        rule_action_overrides=[
                            wafv2.CfnWebACL.RuleActionOverrideProperty(
                                name="SizeRestrictions_BODY",
                                action_to_use=wafv2.CfnWebACL.RuleActionProperty(
                                    count={}
                                ),
                            )
                        ],
                    ),
                    managed("AWSManagedRulesKnownBadInputsRuleSet", 2),
                ],
            )
            arn = acl.attr_arn
        wafv2.CfnWebACLAssociation(
            self,
            "FirewallAssociation",
            resource_arn=self.alb.load_balancer_arn,
            web_acl_arn=arn,
        )

    # -------------------------------------------------------- dns, origin

    def _dns(self) -> None:
        d = self.d
        if not d.ingress.domain or self.zone is None:
            return
        target: route53.RecordTarget
        if self.distribution is not None:
            target = route53.RecordTarget.from_alias(
                route53_targets.CloudFrontTarget(self.distribution)
            )
            route53.AaaaRecord(
                self,
                "AliasV6",
                zone=self.zone,
                record_name=d.ingress.domain,
                target=target,
            )
        else:
            assert self.alb is not None
            target = route53.RecordTarget.from_alias(
                route53_targets.LoadBalancerTarget(self.alb)
            )
        route53.ARecord(
            self, "Alias", zone=self.zone, record_name=d.ingress.domain, target=target
        )

    def _origin_updater(self) -> None:
        distribution = self.distribution
        main = self.services["main"]
        assert distribution is not None
        function = lambda_.Function(
            self,
            "OriginUpdater",
            log_group=self._function_logs("OriginUpdaterLogs"),
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="index.handler",
            code=lambda_.Code.from_inline(origin_updater.CODE),
            timeout=cdk.Duration.seconds(60),
            description="Points the CloudFront origin at the running n8n main task",
            environment={
                "CLUSTER": self.cluster.cluster_name,
                "SERVICE": main.service_name,
                "DISTRIBUTION_ID": distribution.distribution_id,
                "ORIGIN_ID": TASK_ORIGIN_ID,
            },
        )
        self.origin_function = function
        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "ecs:ListTasks",
                    "ecs:DescribeTasks",
                    "ec2:DescribeNetworkInterfaces",
                ],
                resources=["*"],
            )
        )
        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudfront:GetDistributionConfig",
                    "cloudfront:UpdateDistribution",
                ],
                resources=[
                    self.format_arn(
                        service="cloudfront",
                        region="",
                        resource="distribution",
                        resource_name=distribution.distribution_id,
                    )
                ],
            )
        )
        events.Rule(
            self,
            "MainTaskRunning",
            event_pattern=events.EventPattern(
                source=["aws.ecs"],
                detail_type=["ECS Task State Change"],
                detail={
                    "clusterArn": [self.cluster.cluster_arn],
                    "group": [f"service:{main.service_name}"],
                    "lastStatus": ["RUNNING"],
                },
            ),
            targets=[targets.LambdaFunction(function)],
        )
        provider = cr.Provider(
            self,
            "OriginUpdaterProvider",
            on_event_handler=function,
            log_group=self._function_logs("OriginUpdaterProviderLogs"),
        )
        fingerprint = hashlib.sha256(
            self.d.model_dump_json(by_alias=True).encode()
        ).hexdigest()
        update = cdk.CustomResource(
            self,
            "PointOrigin",
            service_token=provider.service_token,
            properties={"configuration": fingerprint},
        )
        update.node.add_dependency(main)
        update.node.add_dependency(distribution)

    def _function_logs(self, construct_id: str) -> logs.LogGroup:
        """A log group deleted with the stack, unlike Lambda's implicit ones."""
        return logs.LogGroup(
            self,
            construct_id,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

    # ---------------------------------------------------------- operations

    def _budget(self) -> None:
        d = self.d
        spec = d.finops.budget
        if spec.monthly_usd is None:
            return
        budgets.CfnBudget(
            self,
            "Budget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=f"{d.name}-monthly",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=spec.monthly_usd, unit="USD"
                ),
                cost_filters={"TagKeyValue": [f"user:n8n-on-aws:deployment${d.name}"]},
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type=kind,
                        threshold=threshold,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            address=email, subscription_type="EMAIL"
                        )
                        for email in spec.alert_emails
                    ],
                )
                for kind, threshold in (("ACTUAL", 80), ("FORECASTED", 100))
            ]
            if spec.alert_emails
            else None,
        )

    def _outputs(self) -> None:
        cdk.CfnOutput(self, "Url", value=self.url, description="n8n editor URL")
        if self.distribution is not None:
            cdk.CfnOutput(
                self, "DistributionId", value=self.distribution.distribution_id
            )
        if self.alb is not None:
            cdk.CfnOutput(
                self, "LoadBalancerDns", value=self.alb.load_balancer_dns_name
            )


def _visibility(name: str) -> wafv2.CfnWebACL.VisibilityConfigProperty:
    return wafv2.CfnWebACL.VisibilityConfigProperty(
        cloud_watch_metrics_enabled=True,
        metric_name=name,
        sampled_requests_enabled=True,
    )
