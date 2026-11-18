"""Lambda code that points the CloudFront ``task`` origin at the running main task.

The same handler serves two triggers: ECS task state changes (EventBridge), and a
CloudFormation custom resource run at each deployment that changes the stack, so
that a distribution update never leaves the origin on its placeholder.
"""

CODE = """
import os

import boto3

ecs = boto3.client("ecs")
ec2 = boto3.client("ec2")
cloudfront = boto3.client("cloudfront")


def _eni(item):
    for attachment in item.get("attachments", []):
        for detail in attachment.get("details", []):
            if detail.get("name") == "networkInterfaceId":
                return detail.get("value")
    return None


def _public_dns(eni):
    found = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni])
    association = found["NetworkInterfaces"][0].get("Association", {})
    return association.get("PublicDnsName") or None


def _running_task_dns():
    cluster = os.environ["CLUSTER"]
    arns = ecs.list_tasks(
        cluster=cluster, serviceName=os.environ["SERVICE"], desiredStatus="RUNNING"
    )["taskArns"]
    if not arns:
        return None
    tasks = ecs.describe_tasks(cluster=cluster, tasks=arns)["tasks"]
    tasks = [t for t in tasks if t.get("lastStatus") == "RUNNING"]
    tasks.sort(key=lambda t: str(t.get("startedAt", "")), reverse=True)
    for task in tasks:
        eni = _eni(task)
        dns = _public_dns(eni) if eni else None
        if dns:
            return dns
    return None


def _point_origin(dns):
    distribution = os.environ["DISTRIBUTION_ID"]
    current = cloudfront.get_distribution_config(Id=distribution)
    config = current["DistributionConfig"]
    changed = False
    for origin in config["Origins"]["Items"]:
        if origin["Id"] == os.environ["ORIGIN_ID"] and origin["DomainName"] != dns:
            origin["DomainName"] = dns
            changed = True
    if changed:
        cloudfront.update_distribution(
            Id=distribution, IfMatch=current["ETag"], DistributionConfig=config
        )
    print({"origin": dns, "changed": changed})
    return changed


def handler(event, context):
    if "RequestType" in event:
        if event["RequestType"] != "Delete":
            dns = _running_task_dns()
            if dns:
                _point_origin(dns)
        return {"PhysicalResourceId": "n8n-origin-updater"}
    detail = event.get("detail", {})
    eni = _eni(detail)
    dns = (_public_dns(eni) if eni else None) or _running_task_dns()
    if dns:
        _point_origin(dns)
    return {"origin": dns}
"""
