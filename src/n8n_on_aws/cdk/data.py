"""The ``<name>-data`` stack: database, queue, files, bucket, secrets, backups."""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_backup as backup
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_efs as efs
from aws_cdk import aws_elasticache as elasticache
from aws_cdk import aws_events as events
from aws_cdk import aws_kms as kms
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from ..config.models import Deployment
from .helpers import aurora_version, import_secret, postgres_version
from .network import NetworkStack

_NODE_UID = "1000"  # the "node" user in the n8n image


class DataStack(cdk.Stack):
    """State that outlives n8n tasks. Most of it is retained on deletion."""

    kms_key: kms.IKey | None
    db_endpoint: str | None
    db_secret: secretsmanager.ISecret | None
    db_construct: Construct | None
    queue_endpoint: str | None
    file_system: efs.FileSystem | None
    access_point: efs.AccessPoint | None
    bucket: s3.IBucket | None
    encryption_key: secretsmanager.ISecret
    runners_token: secretsmanager.ISecret

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        d: Deployment,
        network: NetworkStack,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.d = d
        self.network = network
        self.dev = d.environment == "dev"
        self._kms()
        self._secrets()
        self._database()
        self._queue()
        self._files()
        self._bucket()
        self._backup()

    def _retain(self) -> cdk.RemovalPolicy:
        return cdk.RemovalPolicy.DESTROY if self.dev else cdk.RemovalPolicy.RETAIN

    # ---------------------------------------------------------- kms, secrets

    def _kms(self) -> None:
        spec = self.d.aws.kms_key
        if spec.kind == "create":
            self.kms_key = kms.Key(
                self,
                "Key",
                alias=f"alias/{self.d.name}",
                enable_key_rotation=True,
                removal_policy=cdk.RemovalPolicy.RETAIN,
            )
        elif spec.kind == "existing":
            self.kms_key = kms.Key.from_key_arn(self, "Key", spec.arn)
        else:
            self.kms_key = None

    def _secrets(self) -> None:
        d = self.d
        if d.n8n.encryption_key.kind == "existing":
            self.encryption_key = import_secret(
                self, "EncryptionKey", d.n8n.encryption_key.secret
            )
        else:
            secret = secretsmanager.Secret(
                self,
                "EncryptionKey",
                secret_name=f"{d.name}/n8n-encryption-key",
                description="n8n encryption key; losing it loses all credentials",
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    exclude_punctuation=True, password_length=48
                ),
                encryption_key=self.kms_key,
            )
            secret.apply_removal_policy(cdk.RemovalPolicy.RETAIN)
            self.encryption_key = secret
        self.runners_token = secretsmanager.Secret(
            self,
            "RunnersToken",
            secret_name=f"{d.name}/n8n-runners-token",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                exclude_punctuation=True, password_length=48
            ),
            encryption_key=self.kms_key,
        )

    # -------------------------------------------------------------- database

    def _database(self) -> None:
        d = self.d
        spec = d.database
        self.db_endpoint = None
        self.db_secret = None
        self.db_construct = None
        net = self.network
        if spec.kind == "external":
            if spec.security_group_id:
                ec2.CfnSecurityGroupIngress(
                    self,
                    "ExternalDbIngress",
                    group_id=spec.security_group_id,
                    ip_protocol="tcp",
                    from_port=spec.port,
                    to_port=spec.port,
                    source_security_group_id=net.sg_tasks.security_group_id,
                    description=f"n8n {d.name}",
                )
            return
        if spec.kind == "sqlite":
            return
        removal = cdk.RemovalPolicy.DESTROY if self.dev else cdk.RemovalPolicy.SNAPSHOT
        credentials = rds.Credentials.from_generated_secret(
            "n8n", secret_name=f"{d.name}/database", encryption_key=self.kms_key
        )
        common: dict[str, Any] = {
            "vpc": net.vpc,
            "vpc_subnets": net.data_subnets,
            "security_groups": [net.sg_data],
            "credentials": credentials,
            "storage_encrypted": True,
            "storage_encryption_key": self.kms_key,
            "deletion_protection": spec.deletion_protection,
            "removal_policy": removal,
            "backup": rds.BackupProps(
                retention=cdk.Duration.days(spec.backup_retention_days)
            ),
        }
        if spec.kind in ("aurora-serverless", "aurora"):
            engine = rds.DatabaseClusterEngine.aurora_postgres(
                version=aurora_version(spec.engine_version)
            )
            if spec.kind == "aurora-serverless":
                writer = rds.ClusterInstance.serverless_v2("writer")
                readers = [
                    rds.ClusterInstance.serverless_v2(
                        f"reader{i + 1}", scale_with_writer=True
                    )
                    for i in range(spec.readers)
                ]
                sizing: dict[str, Any] = {
                    "serverless_v2_min_capacity": spec.min_acu,
                    "serverless_v2_max_capacity": spec.max_acu,
                }
            else:
                instance_type = ec2.InstanceType(
                    spec.instance_class.removeprefix("db.")
                )
                writer = rds.ClusterInstance.provisioned(
                    "writer", instance_type=instance_type
                )
                readers = [
                    rds.ClusterInstance.provisioned(
                        f"reader{i + 1}", instance_type=instance_type
                    )
                    for i in range(spec.instances - 1)
                ]
                sizing = {}
                if spec.io_optimized:
                    sizing["storage_type"] = rds.DBClusterStorageType.AURORA_IOPT1
            cluster = rds.DatabaseCluster(
                self,
                "Database",
                engine=engine,
                writer=writer,
                readers=readers,
                default_database_name="n8n",
                **sizing,
                **common,
            )
            self.db_endpoint = cluster.cluster_endpoint.hostname
            self.db_secret = cluster.secret
            self.db_construct = cluster
        else:
            assert spec.kind == "rds"
            instance = rds.DatabaseInstance(
                self,
                "Database",
                engine=rds.DatabaseInstanceEngine.postgres(
                    version=postgres_version(spec.engine_version)
                ),
                instance_type=ec2.InstanceType(spec.instance_class.removeprefix("db.")),
                allocated_storage=spec.storage_gb,
                storage_type=rds.StorageType.GP3,
                multi_az=spec.multi_az,
                database_name="n8n",
                backup_retention=cdk.Duration.days(spec.backup_retention_days),
                **{key: value for key, value in common.items() if key != "backup"},
            )
            self.db_endpoint = instance.db_instance_endpoint_address
            self.db_secret = instance.secret
            self.db_construct = instance

    # ----------------------------------------------------------------- queue

    def _queue(self) -> None:
        d = self.d
        spec = d.queue
        net = self.network
        self.queue_endpoint = None
        if not d.is_queued or spec.kind == "none":
            return
        if spec.kind == "external":
            if spec.security_group_id:
                ec2.CfnSecurityGroupIngress(
                    self,
                    "ExternalRedisIngress",
                    group_id=spec.security_group_id,
                    ip_protocol="tcp",
                    from_port=spec.port,
                    to_port=spec.port,
                    source_security_group_id=net.sg_tasks.security_group_id,
                    description=f"n8n {d.name}",
                )
            return
        subnet_ids = net.vpc.select_subnets(
            **_selection_kwargs(net.data_subnets)
        ).subnet_ids
        kms_id = self.kms_key.key_arn if self.kms_key else None
        if spec.kind == "elasticache-serverless":
            cache = elasticache.CfnServerlessCache(
                self,
                "Queue",
                serverless_cache_name=f"{d.name}-queue",
                engine=spec.engine,
                security_group_ids=[net.sg_data.security_group_id],
                subnet_ids=subnet_ids,
                kms_key_id=kms_id,
                description=f"n8n queue for {d.name}",
            )
            self.queue_endpoint = cache.attr_endpoint_address
            return
        subnet_group = elasticache.CfnSubnetGroup(
            self,
            "QueueSubnets",
            description=f"n8n queue for {d.name}",
            subnet_ids=subnet_ids,
        )
        group = elasticache.CfnReplicationGroup(
            self,
            "Queue",
            replication_group_description=f"n8n queue for {d.name}",
            engine=spec.engine,
            cache_node_type=spec.node_type,
            num_cache_clusters=1 + spec.replicas,
            automatic_failover_enabled=spec.replicas > 0,
            multi_az_enabled=spec.replicas > 0,
            transit_encryption_enabled=True,
            at_rest_encryption_enabled=True,
            kms_key_id=kms_id,
            cache_subnet_group_name=subnet_group.ref,
            security_group_ids=[net.sg_data.security_group_id],
        )
        self.queue_endpoint = group.attr_primary_end_point_address

    # ----------------------------------------------------------- files (EFS)

    def _files(self) -> None:
        self.file_system = None
        self.access_point = None
        if not self.d.needs_efs:
            return
        net = self.network
        file_system = efs.FileSystem(
            self,
            "Files",
            vpc=net.vpc,
            vpc_subnets=net.data_subnets,
            security_group=net.sg_data,
            encrypted=True,
            kms_key=self.kms_key,
            lifecycle_policy=efs.LifecyclePolicy.AFTER_30_DAYS,
            throughput_mode=efs.ThroughputMode.ELASTIC,
            removal_policy=self._retain(),
        )
        self.access_point = file_system.add_access_point(
            "N8nHome",
            path="/n8n",
            create_acl=efs.Acl(
                owner_uid=_NODE_UID, owner_gid=_NODE_UID, permissions="750"
            ),
            posix_user=efs.PosixUser(uid=_NODE_UID, gid=_NODE_UID),
        )
        self.file_system = file_system

    # ---------------------------------------------------------------- bucket

    def _bucket(self) -> None:
        d = self.d
        spec = d.storage.bucket
        self.bucket = None
        if spec.kind == "existing":
            self.bucket = s3.Bucket.from_bucket_name(self, "Bucket", spec.name)
            return
        if spec.kind != "create":
            return
        rules: list[s3.LifecycleRule] = [
            s3.LifecycleRule(
                abort_incomplete_multipart_upload_after=cdk.Duration.days(7)
            )
        ]
        # n8n delegates binary-data pruning to the bucket, but execution data must
        # never be expired by a lifecycle rule (see docs/research/03).
        if (
            d.binary_mode == "s3"
            and d.storage.execution_data == "database"
            and spec.binary_expiry_days
        ):
            rules.append(
                s3.LifecycleRule(
                    prefix="workflows/",
                    expiration=cdk.Duration.days(spec.binary_expiry_days),
                )
            )
        exchange = d.storage.exchange
        if exchange.enabled and exchange.expiry_days:
            rules.append(
                s3.LifecycleRule(
                    prefix=exchange.prefix,
                    expiration=cdk.Duration.days(exchange.expiry_days),
                )
            )
        self.bucket = s3.Bucket(
            self,
            "Bucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            encryption=s3.BucketEncryption.KMS
            if self.kms_key
            else s3.BucketEncryption.S3_MANAGED,
            encryption_key=self.kms_key,
            bucket_key_enabled=self.kms_key is not None,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            lifecycle_rules=rules,
            removal_policy=self._retain(),
            auto_delete_objects=self.dev,
        )

    # ---------------------------------------------------------------- backup

    def _backup(self) -> None:
        d = self.d
        if d.backup.kind != "aws-backup":
            return
        resources = [
            backup.BackupResource.from_construct(item)
            for item in (self.db_construct, self.file_system)
            if item is not None
        ]
        if not resources:
            return
        plan = backup.BackupPlan(self, "Backup", backup_plan_name=f"{d.name}-backup")
        plan.add_rule(
            backup.BackupPlanRule(
                rule_name="daily",
                delete_after=cdk.Duration.days(d.backup.retention_days),
                schedule_expression=events.Schedule.cron(hour="3", minute="0"),
            )
        )
        plan.add_selection("Data", resources=resources)


def _selection_kwargs(selection: ec2.SubnetSelection) -> dict[str, Any]:
    if selection.subnets:
        return {"subnets": selection.subnets}
    return {"subnet_type": selection.subnet_type}
