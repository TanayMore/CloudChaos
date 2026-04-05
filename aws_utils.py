"""
AWS helpers for Cloud Chaos Lab.

All AWS interactions go through boto3. Uses the default credential chain (shared config,
environment variables, IAM role, etc.). Region defaults to ap-south-1 unless overridden.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError

# Single default for beginner demos (override with AWS_REGION / AWS_DEFAULT_REGION).
DEFAULT_AWS_REGION = "ap-south-1"

# SSM Parameter Store paths for latest Amazon Linux AMIs (region-specific values).
_SSM_AL2023_X86 = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
_SSM_AL2_X86 = "/aws/service/ami-amazon-linux-latest/amzn2-ami-hvm-x86_64-gp2"


def default_region() -> str:
    return (
        os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or DEFAULT_AWS_REGION
    )


def make_session(region_name: Optional[str] = None) -> boto3.session.Session:
    # boto3 will automatically read AWS_* env vars (including STS token) if present.
    return boto3.session.Session(region_name=region_name or default_region())


def validate_region_name(region: str) -> None:
    """Raise ValueError with a clear message if the region string is invalid or unknown."""
    r = (region or "").strip()
    if not r:
        raise ValueError("Region is empty.")
    # describe_regions accepts RegionNames filter; invalid names cause ClientError.
    ec2 = boto3.client("ec2", region_name=default_region())
    try:
        resp = ec2.describe_regions(RegionNames=[r])
        if not resp.get("Regions"):
            raise ValueError(f"Unknown AWS region: {r}")
    except (NoCredentialsError, PartialCredentialsError) as e:
        raise ValueError(format_aws_error(e)) from e
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "AuthFailure":
            raise ValueError(
                "Cannot validate region: AWS credentials rejected. "
                "Fix credentials and ensure they match the account you intend to use."
            ) from e
        raise ValueError(format_aws_error(e)) from e


def get_latest_amazon_linux_ami_id(session: boto3.session.Session) -> str:
    """
    Resolve the latest Amazon Linux AMI for the session's region via SSM Parameter Store.
    Falls back from AL2023 to AL2 if a path is missing in the region.
    """
    ssm = session.client("ssm")
    for name in (_SSM_AL2023_X86, _SSM_AL2_X86):
        try:
            resp = ssm.get_parameter(Name=name)
            val = (resp.get("Parameter") or {}).get("Value")
            if val:
                return str(val).strip()
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ParameterNotFound":
                continue
            raise ValueError(format_aws_error(e)) from e
    raise ValueError(
        "Could not resolve an Amazon Linux AMI in this region via SSM. "
        "Try another region or check that SSM is available."
    )


def check_credentials(session: boto3.session.Session) -> Tuple[bool, Optional[str]]:
    """Returns (ok, error_message). Uses STS GetCallerIdentity."""
    try:
        sts = session.client("sts")
        sts.get_caller_identity()
        return True, None
    except (NoCredentialsError, PartialCredentialsError) as e:
        return False, format_aws_error(e)
    except ClientError as e:
        return False, format_aws_error(e)
    except Exception as e:
        return False, str(e)


def format_aws_error(exc: BaseException) -> str:
    """Turn boto3/botocore errors into short, UI-friendly messages."""
    if isinstance(exc, NoCredentialsError):
        return (
            "Missing AWS credentials. Set up ~/.aws/credentials, environment variables "
            "(AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY), or an IAM role."
        )
    if isinstance(exc, PartialCredentialsError):
        return (
            "Incomplete AWS credentials (e.g. missing secret key). "
            "Check ~/.aws/credentials or environment variables."
        )
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        msg = exc.response.get("Error", {}).get("Message", str(exc))
        if code == "InvalidAMIID.NotFound":
            return (
                "AMI not found in this region. The app resolves Amazon Linux automatically; "
                "if this persists, check region settings."
            )
        if code in ("UnauthorizedOperation", "AccessDenied", "AccessDeniedException"):
            return f"Permission denied ({code}): {msg}"
        if code in ("AuthFailure", "InvalidClientTokenId", "SignatureDoesNotMatch"):
            return f"Invalid or rejected AWS credentials ({code}): {msg}"
        if code == "OptInRequired":
            return (
                "This account must opt in to use the selected region. "
                "Enable the region in the AWS console or pick another region."
            )
        return f"AWS error ({code}): {msg}"
    return str(exc)


def ec2_get_instance_state(ec2: Any, instance_id: str) -> Optional[str]:
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    reservations = resp.get("Reservations", [])
    if not reservations:
        return None

    instances = reservations[0].get("Instances", [])
    if not instances:
        return None

    return instances[0]["State"]["Name"]


def ec2_launch_instance(
    ec2: Any,
    *,
    ami_id: str,
    instance_type: str,
    key_name: Optional[str],
    security_group_ids: List[str],
    subnet_id: Optional[str],
    iam_instance_profile: Optional[str] = None,
    tag_name: Optional[str] = None,
) -> str:
    params: Dict[str, Any] = {
        "ImageId": ami_id,
        "InstanceType": instance_type,
        "SecurityGroupIds": security_group_ids,
        "MinCount": 1,
        "MaxCount": 1,
    }

    if key_name:
        params["KeyName"] = key_name
    if subnet_id:
        params["SubnetId"] = subnet_id
    if iam_instance_profile:
        params["IamInstanceProfile"] = {"Name": iam_instance_profile}

    if tag_name:
        params["TagSpecifications"] = [
            {
                "ResourceType": "instance",
                "Tags": [{"Key": "Name", "Value": tag_name}],
            }
        ]

    resp = ec2.run_instances(**params)
    return resp["Instances"][0]["InstanceId"]


def ec2_control_instance(ec2: Any, instance_id: str, action: str) -> None:
    action = action.lower().strip()

    if action == "start":
        ec2.start_instances(InstanceIds=[instance_id])
        return
    if action == "stop":
        ec2.stop_instances(InstanceIds=[instance_id])
        return
    if action == "reboot":
        ec2.reboot_instances(InstanceIds=[instance_id])
        return

    raise ValueError(f"Unsupported EC2 action: {action}")


def cloudwatch_get_cpu_timeseries(
    cloudwatch: Any,
    *,
    instance_id: str,
    period_seconds: int,
    datapoints: int,
) -> List[float]:
    # CPU is returned oldest -> newest.
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(seconds=period_seconds * datapoints)
    resp = cloudwatch.get_metric_statistics(
        Namespace="AWS/EC2",
        MetricName="CPUUtilization",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=start_time,
        EndTime=end_time,
        Period=period_seconds,
        Statistics=["Average"],
    )

    datapoints_list = resp.get("Datapoints", [])
    datapoints_list.sort(key=lambda d: d["Timestamp"])

    values: List[float] = []
    for d in datapoints_list[-datapoints:]:
        if "Average" in d and d["Average"] is not None:
            values.append(float(d["Average"]))
    return values


def cloudwatch_get_latest_cpu_and_timeseries(
    cloudwatch: Any,
    *,
    instance_id: str,
    period_seconds: int,
    datapoints: int,
) -> Dict[str, Any]:
    values = cloudwatch_get_cpu_timeseries(
        cloudwatch,
        instance_id=instance_id,
        period_seconds=period_seconds,
        datapoints=datapoints,
    )
    latest = values[-1] if values else None
    return {"latest_cpu_percent": latest, "cpu_series": values}


def cloudwatch_get_instance_status_checks(cloudwatch: Any, *, instance_id: str) -> Dict[str, Any]:
    # 1.0 means failed (for StatusCheckFailed_* metrics). We show both system + instance.
    def latest_failed(metric_name: str) -> Optional[float]:
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(minutes=5)
        resp = cloudwatch.get_metric_statistics(
            Namespace="AWS/EC2",
            MetricName=metric_name,
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=start_time,
            EndTime=end_time,
            Period=60,
            Statistics=["Maximum"],
        )
        datapoints_list = resp.get("Datapoints", [])
        if not datapoints_list:
            return None
        datapoints_list.sort(key=lambda d: d["Timestamp"])
        last = datapoints_list[-1]
        return float(last["Maximum"]) if last.get("Maximum") is not None else None

    system_failed = latest_failed("StatusCheckFailed_System")
    instance_failed = latest_failed("StatusCheckFailed_Instance")

    return {
        "status_check_failed_system": system_failed,
        "status_check_failed_instance": instance_failed,
        "status_ok": (system_failed in (0.0, None)) and (instance_failed in (0.0, None)),
    }


def sns_ensure_topic_and_subscription(
    sns: Any,
    *,
    topic_name: str,
    email: str,
) -> str:
    topic_arn = sns.create_topic(Name=topic_name)["TopicArn"]

    # Check if a subscription already exists for that email endpoint.
    subs = sns.list_subscriptions_by_topic(TopicArn=topic_arn).get("Subscriptions", [])
    for sub in subs:
        if sub.get("Endpoint") == email:
            return topic_arn

    sns.subscribe(
        TopicArn=topic_arn,
        Protocol="email",
        Endpoint=email,
    )
    return topic_arn


def sns_send_alert(sns: Any, *, topic_arn: str, subject: str, message: str) -> None:
    sns.publish(
        TopicArn=topic_arn,
        Subject=subject[:100],
        Message=message,
    )


def lambda_invoke(lambda_client: Any, *, function_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    # Included for completeness since the project scope mentions Lambda,
    # but Cloud Chaos Lab does not require Lambda for the core demo.
    resp = lambda_client.invoke(
        FunctionName=function_name,
        Payload=json.dumps(payload).encode("utf-8"),
        InvocationType="RequestResponse",
    )
    return {"status_code": resp.get("StatusCode")}

