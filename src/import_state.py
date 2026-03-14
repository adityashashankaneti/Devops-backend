"""
Import existing Terraform infrastructure from S3 state files into the frontend canvas format.

Endpoint: GET /api/import?project=<name>&region=<region>

Reads terraform.tfstate files from the project's S3 remote state backend,
parses AWS resources, and returns ReactFlow nodes/edges compatible with the frontend canvas.
"""

import json
import logging
import boto3
from typing import Any

logger = logging.getLogger(__name__)

# Terraform resource type → frontend canvas resource ID
# aws_subnet is handled separately (public vs. private)
TF_TYPE_TO_RESOURCE_ID: dict[str, str] = {
    "aws_vpc":                       "vpc",
    "aws_internet_gateway":          "internet-gateway",
    "aws_nat_gateway":               "nat-gateway",
    "aws_instance":                  "ec2",
    "aws_lambda_function":           "lambda",
    "aws_ecs_cluster":               "ecs",
    "aws_s3_bucket":                 "s3",
    "aws_elasticache_cluster":       "elasticache",
    "aws_db_instance":               "rds",
    "aws_dynamodb_table":            "dynamodb",
    "aws_lb":                        "alb",
    "aws_alb":                       "alb",
    "aws_sqs_queue":                 "sqs",
    "aws_sns_topic":                 "sns",
    "aws_cloudwatch_event_rule":     "eventbridge",
    "aws_cloudfront_distribution":   "cloudfront",
    "aws_route53_zone":              "route53",
    "aws_security_group":            "security-group",
}

# Types that produce no canvas node (config-only resources)
SKIP_TYPES = {
    "aws_route_table",
    "aws_route_table_association",
    "aws_main_route_table_association",
    "aws_internet_gateway_attachment",
    "aws_security_group_rule",
    "aws_iam_role",
    "aws_iam_role_policy",
    "aws_iam_role_policy_attachment",
}

# Matches awsResources.ts exactly — used to build the node `data` payload
RESOURCE_META: dict[str, dict] = {
    "vpc": {
        "name": "VPC", "abbr": "VPC", "color": "#8B5CF6",
        "category": "networking", "description": "Virtual Private Cloud", "nodeType": "container",
    },
    "subnet-public": {
        "name": "Public Subnet", "abbr": "PUB", "color": "#06B6D4",
        "category": "networking", "description": "Public-facing subnet", "nodeType": "container",
    },
    "subnet-private": {
        "name": "Private Subnet", "abbr": "PRV", "color": "#0891B2",
        "category": "networking", "description": "Private subnet (no direct internet)", "nodeType": "container",
    },
    "internet-gateway": {
        "name": "Internet Gateway", "abbr": "IGW", "color": "#6D28D9",
        "category": "networking", "description": "Connect VPC to Internet",
    },
    "nat-gateway": {
        "name": "NAT Gateway", "abbr": "NAT", "color": "#7C3AED",
        "category": "networking", "description": "Network Address Translation",
    },
    "ec2": {
        "name": "EC2", "abbr": "EC2", "color": "#F59E0B",
        "category": "compute", "description": "Elastic Compute Cloud",
    },
    "lambda": {
        "name": "Lambda", "abbr": "λ", "color": "#D97706",
        "category": "compute", "description": "Serverless Functions",
    },
    "ecs": {
        "name": "ECS", "abbr": "ECS", "color": "#B45309",
        "category": "compute", "description": "Elastic Container Service",
    },
    "s3": {
        "name": "S3", "abbr": "S3", "color": "#10B981",
        "category": "storage", "description": "Simple Storage Service",
    },
    "elasticache": {
        "name": "ElastiCache", "abbr": "ELC", "color": "#1D4ED8",
        "category": "database", "description": "In-Memory Cache",
    },
    "rds": {
        "name": "RDS", "abbr": "RDS", "color": "#3B82F6",
        "category": "database", "description": "Relational Database Service",
    },
    "dynamodb": {
        "name": "DynamoDB", "abbr": "DDB", "color": "#2563EB",
        "category": "database", "description": "NoSQL Database",
    },
    "alb": {
        "name": "App Load Balancer", "abbr": "ALB", "color": "#EC4899",
        "category": "load-balancing", "description": "Application Load Balancer",
    },
    "sqs": {
        "name": "SQS", "abbr": "SQS", "color": "#EA580C",
        "category": "messaging", "description": "Simple Queue Service",
    },
    "sns": {
        "name": "SNS", "abbr": "SNS", "color": "#C2410C",
        "category": "messaging", "description": "Simple Notification Service",
    },
    "eventbridge": {
        "name": "EventBridge", "abbr": "EB", "color": "#9A3412",
        "category": "messaging", "description": "Serverless Event Bus",
    },
    "cloudfront": {
        "name": "CloudFront", "abbr": "CF", "color": "#F97316",
        "category": "messaging", "description": "Content Delivery Network",
    },
    "route53": {
        "name": "Route 53", "abbr": "R53", "color": "#5B21B6",
        "category": "networking", "description": "DNS Web Service", "nodeType": "expandable",
    },
    "security-group": {
        "name": "Security Group", "abbr": "SG", "color": "#EF4444",
        "category": "security", "description": "Virtual Firewall Rules", "nodeType": "container",
    },
}


def _get_account_id() -> str:
    sts = boto3.client("sts")
    return sts.get_caller_identity()["Account"]


def _list_state_files(s3: Any, bucket: str, prefix: str) -> list[str]:
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("/terraform.tfstate"):
                keys.append(obj["Key"])
    return keys


def _parse_state_file(s3: Any, bucket: str, key: str) -> list[dict]:
    """Download and parse a terraform.tfstate, returning flat list of resource instance dicts."""
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        state = json.loads(resp["Body"].read())
    except Exception as e:
        logger.warning("Failed to read state file %s: %s", key, e)
        return []

    resources = []
    for res in state.get("resources", []):
        if res.get("mode") != "managed":
            continue
        tf_type = res.get("type", "")
        name = res.get("name", "")
        for instance in res.get("instances", []):
            attrs = instance.get("attributes", {})
            resources.append({"tf_type": tf_type, "name": instance.get("index_key") or name, "attrs": attrs})
    return resources


def _resolve_subnet_type(attrs: dict) -> str:
    return "subnet-public" if attrs.get("map_public_ip_on_launch") else "subnet-private"


def _make_node(
    node_id: str,
    resource_id: str,
    instance_name: str,
    config: dict,
    parent_id: str | None = None,
) -> dict:
    meta = RESOURCE_META.get(resource_id, {
        "name": resource_id, "abbr": resource_id[:3].upper(),
        "color": "#6B7280", "category": "other", "description": resource_id,
    })
    node_type_label = meta.get("nodeType", "")
    if node_type_label == "container":
        react_flow_type = "containerNode"
    elif node_type_label == "expandable":
        react_flow_type = "route53Node"
    else:
        react_flow_type = "awsNode"

    node: dict = {
        "id": node_id,
        "type": react_flow_type,
        "position": {"x": 0, "y": 0},  # set by caller
        "data": {
            "id": resource_id,
            "name": meta["name"],
            "category": meta["category"],
            "description": meta["description"],
            "color": meta["color"],
            "abbr": meta["abbr"],
            "config": {"name": instance_name, **config},
            **({"nodeType": node_type_label} if node_type_label else {}),
        },
    }
    if react_flow_type == "containerNode":
        node["style"] = {"width": 320, "height": 200}
    if parent_id:
        node["parentNode"] = parent_id
        node["extent"] = "parent"
    return node


def import_from_state(project: str, region: str) -> dict:
    """
    Read Terraform state files from S3 and return ReactFlow-compatible nodes/edges.
    """
    s3 = boto3.client("s3", region_name=region)

    try:
        account_id = _get_account_id()
    except Exception as e:
        return {"error": f"Could not get AWS account ID: {e}", "nodes": [], "edges": []}

    bucket = f"{project}-tf-state-{account_id}"
    # Terragrunt state key = path_relative_to_include()/terraform.tfstate
    # Root hcl is at environments/terragrunt.hcl, child is at environments/dev/<type>/
    # → relative path = dev/<type>  → S3 key = dev/<type>/terraform.tfstate
    prefix = "dev/"

    try:
        state_keys = _list_state_files(s3, bucket, prefix)
    except Exception as e:
        return {"error": f"Could not list S3 bucket '{bucket}': {e}", "nodes": [], "edges": []}

    if not state_keys:
        return {
            "error": f"No Terraform state files found in s3://{bucket}/{prefix}. "
                     "Deploy your infrastructure first.",
            "nodes": [],
            "edges": [],
        }

    # Parse all state files
    all_resources: list[dict] = []
    for key in state_keys:
        all_resources.extend(_parse_state_file(s3, bucket, key))

    logger.info("import_from_state: project=%s found %d resources in %d state files",
                project, len(all_resources), len(state_keys))

    # Separate by Terraform type
    vpcs     = [r for r in all_resources if r["tf_type"] == "aws_vpc"]
    subnets  = [r for r in all_resources if r["tf_type"] == "aws_subnet"]
    others   = [r for r in all_resources if r["tf_type"] not in ("aws_vpc", "aws_subnet") and r["tf_type"] not in SKIP_TYPES]

    nodes: list[dict] = []
    node_counter = 0

    # AWS VPC ID (e.g. "vpc-0abc") → canvas node ID
    vpc_aws_id_to_node: dict[str, str] = {}

    # ── VPC nodes ────────────────────────────────────────────────────────────
    VPC_WIDTH, VPC_HEIGHT = 640, 520
    VPC_GAP = 80

    for i, vpc in enumerate(vpcs):
        node_id = f"import-{node_counter}"; node_counter += 1
        attrs = vpc["attrs"]
        vpc_aws_id_to_node[attrs.get("id", "")] = node_id

        node = _make_node(node_id, "vpc", vpc["name"], {
            "cidrBlock":    attrs.get("cidr_block", ""),
            "dnsHostnames": attrs.get("enable_dns_hostnames", True),
            "dnsSupport":   attrs.get("enable_dns_support", True),
        })
        node["position"] = {"x": 50 + i * (VPC_WIDTH + VPC_GAP), "y": 50}
        node["style"]    = {"width": VPC_WIDTH, "height": VPC_HEIGHT}
        nodes.append(node)

    # ── Subnet nodes (inside VPCs) ────────────────────────────────────────────
    SUBNET_WIDTH, SUBNET_HEIGHT = 270, 160
    SUBNET_PAD_X, SUBNET_PAD_Y, SUBNET_GAP = 20, 70, 20

    subnets_by_vpc: dict[str, list] = {}
    for subnet in subnets:
        key = subnet["attrs"].get("vpc_id", "__orphan__")
        subnets_by_vpc.setdefault(key, []).append(subnet)

    for vpc_aws_id, vpc_subnets in subnets_by_vpc.items():
        parent_id = vpc_aws_id_to_node.get(vpc_aws_id)  # None for orphaned subnets
        orphan_x_start = 50

        for j, subnet in enumerate(vpc_subnets):
            node_id = f"import-{node_counter}"; node_counter += 1
            attrs = subnet["attrs"]
            resource_id = _resolve_subnet_type(attrs)

            if parent_id:
                col = j % 2
                row = j // 2
                pos_x = SUBNET_PAD_X + col * (SUBNET_WIDTH + SUBNET_GAP)
                pos_y = SUBNET_PAD_Y + row * (SUBNET_HEIGHT + SUBNET_GAP)
            else:
                pos_x = orphan_x_start + j * (SUBNET_WIDTH + SUBNET_GAP)
                pos_y = 50 + VPC_HEIGHT + 80

            node = _make_node(node_id, resource_id, subnet["name"], {
                "cidrBlock":          attrs.get("cidr_block", ""),
                "availabilityZone":   attrs.get("availability_zone", ""),
                "mapPublicIpOnLaunch": attrs.get("map_public_ip_on_launch", False),
            }, parent_id=parent_id)
            node["position"] = {"x": pos_x, "y": pos_y}
            node["style"]    = {"width": SUBNET_WIDTH, "height": SUBNET_HEIGHT}
            nodes.append(node)

    # ── Other resources (flat grid below VPCs) ────────────────────────────────
    OTHER_COLS = 5
    OTHER_W, OTHER_H = 110, 70
    OTHER_GAP_X, OTHER_GAP_Y = 40, 30
    other_start_y = 50 + VPC_HEIGHT + 80 if vpcs else 50

    renderable = [r for r in others if TF_TYPE_TO_RESOURCE_ID.get(r["tf_type"])]
    for k, res in enumerate(renderable):
        resource_id = TF_TYPE_TO_RESOURCE_ID[res["tf_type"]]
        node_id = f"import-{node_counter}"; node_counter += 1
        attrs = res["attrs"]

        # Extract common meaningful attributes
        config: dict = {}
        for tf_key, canvas_key in [
            ("cidr_block",       "cidrBlock"),
            ("instance_type",    "instanceType"),
            ("engine",           "engine"),
            ("engine_version",   "engineVersion"),
            ("instance_class",   "instanceClass"),
            ("runtime",          "runtime"),
            ("handler",          "handler"),
            ("memory_size",      "memorySize"),
            ("timeout",          "timeout"),
            ("billing_mode",     "billingMode"),
            ("hash_key",         "hashKey"),
        ]:
            val = attrs.get(tf_key)
            if val is not None:
                config[canvas_key] = val

        col = k % OTHER_COLS
        row = k // OTHER_COLS
        node = _make_node(node_id, resource_id, res["name"], config)
        node["position"] = {
            "x": 50 + col * (OTHER_W + OTHER_GAP_X),
            "y": other_start_y + row * (OTHER_H + OTHER_GAP_Y),
        }
        nodes.append(node)

    return {
        "nodes": nodes,
        "edges": [],
        "resource_count": len(nodes),
        "project": project,
        "region": region,
        "state_files_read": len(state_keys),
    }
