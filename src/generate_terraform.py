"""
Generate per-resource-type YAML configs from the architecture JSON payload using Claude.

Instead of generating raw Terraform code, Claude now classifies each canvas
resource by module type and emits a YAML config dict.  The Python code then
writes the YAML into per-resource-type directories alongside pre-built
Terragrunt configs that point at reusable Terraform modules.

Returns:
    dict[str, dict] — { module_type: { resource_name: { config } } }
    e.g. {"vpc": {"my-vpc": {"cidr_block": "10.0.0.0/16"}}}
"""

import json
import logging
import yaml

logger = logging.getLogger(__name__)

# ── Resource-type mapping ────────────────────────────────────────────────────
# Maps frontend resource IDs → Terraform module directory names.
RESOURCE_TYPE_MAP: dict[str, str] = {
    "vpc":                "vpc",
    "subnet-public":      "subnet",
    "subnet-private":     "subnet",
    "availability-zone":  None,        # placement concept, not a real TF resource
    "nat-gateway":        "nat-gateway",
    "internet-gateway":   "internet-gateway",
    "route53":            "route53",
    "elastic-ip":         None,        # handled inside nat-gateway module
    "transit-gateway":    None,
    "direct-connect":     None,
    "vpc-peering":        None,
    "ec2":                "ec2",
    "lambda":             "lambda",
    "ecs":                "ecs",
    "eks":                None,
    "fargate":            "ecs",
    "auto-scaling":       None,
    "batch":              None,
    "s3":                 "s3",
    "ebs":                None,        # handled as ec2 root_block_device
    "efs":                None,
    "glacier":            None,
    "fsx":                None,
    "rds":                "rds",
    "dynamodb":           "dynamodb",
    "elasticache":        "elasticache",
    "aurora":             "rds",
    "redshift":           None,
    "documentdb":         None,
    "alb":                "alb",
    "nlb":                "alb",
    "api-gateway":        None,
    "global-accelerator": None,
    "iam":                None,
    "waf":                None,
    "shield":             None,
    "kms":                None,
    "secrets-manager":    None,
    "security-group":     "security-group",
    "acm":                None,
    "cloudwatch":         None,
    "cloudtrail":         None,
    "config":             None,
    "xray":               None,
    "cloudfront":         "cloudfront",
    "sqs":                "sqs",
    "sns":                "sns",
    "eventbridge":        "eventbridge",
    "kinesis":            None,
    "codepipeline":       None,
    "codebuild":          None,
    "codedeploy":         None,
    "ecr":                None,
    "terraform":          None,
    "route-table":        "route-table",
}

# Module types we have pre-built Terraform modules for
SUPPORTED_MODULES = {v for v in RESOURCE_TYPE_MAP.values() if v is not None}

# ── Claude prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert AWS infrastructure engineer.  You receive a canvas-designed
architecture (nested JSON) and your job is to CLASSIFY each resource by its
Terraform module type and produce a YAML-friendly configuration dict.

You do NOT generate Terraform or HCL code.  You produce a JSON object where:
- Top-level keys are module types (vpc, subnet, ec2, security-group, s3, rds,
  lambda, alb, eventbridge, sqs, sns, dynamodb, route53, ecs, cloudfront,
  elasticache, internet-gateway, nat-gateway, route-table).
- Under each module type is a map of   resource_name → config_object.
- Config objects use snake_case keys matching the Terraform module variables.

Cross-reference rules (use NAME references, not IDs):
- Subnets:         add "vpc_name" pointing to the parent VPC's resource name
- EC2 instances:   add "subnet_name" + "security_groups" (list of SG names)
- Security groups: add "vpc_name"
- RDS:             add "subnet_names" (list) + "security_groups" (list)
- ALB/NLB:         add "vpc_name" + "subnet_names" (list) + "security_groups" + "instance_names" (list of EC2 resource names that are targets, derived from connections)
- Internet GW:     add "vpc_name"
- NAT GW:          add "subnet_name" (public subnet)
- Lambda (in VPC): add "subnet_names" + "security_groups"

Route Table rules (ALWAYS generate these when subnets exist):
- For every PUBLIC subnet:  create a route table in the "route-table" module with:
    vpc_name, routes: [{cidr_block: "0.0.0.0/0", gateway_type: "igw", gateway_name: <igw-name>}]
    subnet_associations: [<public-subnet-name>, ...]
- For every PRIVATE subnet: create a route table with:
    vpc_name, routes: [{cidr_block: "0.0.0.0/0", gateway_type: "nat", gateway_name: <nat-gw-name>}]
    subnet_associations: [<private-subnet-name>, ...]
- Auto-create an internet-gateway if public subnets exist but none is on the canvas.
- Auto-create a nat-gateway (in a public subnet) if private subnets exist but none is on the canvas.

Property mapping from canvas → module config:
- cidrBlock      → cidr_block
- instanceType   → instance_type
- amiId / ami    → ami
- multiAZ        → multi_az
- dnsHostnames   → enable_dns_hostnames
- publicIp       → map_public_ip_on_launch  /  associate_public_ip_address
- engine         → engine
- engineVersion  → engine_version
- dbName         → db_name
- allocatedStorage → allocated_storage
- name           → (becomes the resource key, not a property)

For NLB, set type: "network" in the config (ALB defaults to "application").

════════════════════════════════════════════════════════════════════════════════
CONNECTION-BASED ACCESS POLICIES (CRITICAL)
════════════════════════════════════════════════════════════════════════════════

Every connection (source → target) on the canvas represents a real dependency.
You MUST generate the appropriate access policies:

1. SECURITY GROUP RULES (network-level)
   Add ingress_rules[] to the TARGET resource's security group config.
   Each rule needs: from_port, to_port, protocol, and EITHER
     - cidr_blocks (for CIDR-based access)
     - source_security_group (SG name, for SG-to-SG access)

   Connection rules:
   - EC2/ECS → RDS (MySQL/Aurora):     port 3306, tcp, source_security_group = source's SG
   - EC2/ECS → RDS (PostgreSQL):       port 5432, tcp, source_security_group = source's SG
   - EC2/ECS → ElastiCache (Redis):    port 6379, tcp, source_security_group = source's SG
   - EC2/ECS → ElastiCache (Memcached): port 11211, tcp, source_security_group = source's SG
   - ALB → EC2/ECS:                    port 80+443, tcp, source_security_group = ALB's SG
   - Public → ALB:                     port 443, tcp, cidr_blocks = ["0.0.0.0/0"]
   - Lambda → RDS/ElastiCache:         same port rules, source_security_group = Lambda's SG

   If a resource doesn't already have a security group, CREATE one in the
   security-group module type (e.g. "web-server-sg") and reference it.

2. IAM POLICIES (identity-based, least-privilege)
   Add iam_policies[] to Lambda/ECS configs.  Each policy needs:
     - sid: unique identifier (e.g. "DynamoDBAccess")
     - actions: list of specific IAM actions (NEVER use wildcards like *)
     - resources: list of ARN patterns

   Connection rules (source → target):
   - Lambda/ECS → DynamoDB:
       actions: ["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem",
                 "dynamodb:DeleteItem","dynamodb:Query","dynamodb:Scan"]
       resources: ["arn:aws:dynamodb:*:*:table/<table-name>",
                   "arn:aws:dynamodb:*:*:table/<table-name>/index/*"]
   - Lambda/ECS → S3:
       actions: ["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:ListBucket"]
       resources: ["arn:aws:s3:::<bucket-name>","arn:aws:s3:::<bucket-name>/*"]
   - Lambda/ECS → SQS:
       actions: ["sqs:SendMessage","sqs:ReceiveMessage","sqs:DeleteMessage",
                 "sqs:GetQueueAttributes"]
       resources: ["arn:aws:sqs:*:*:<queue-name>"]
   - Lambda/ECS → SNS:
       actions: ["sns:Publish"]
       resources: ["arn:aws:sns:*:*:<topic-name>"]
   - Lambda → RDS (via RDS Proxy/IAM auth):
       actions: ["rds-db:connect"]
       resources: ["arn:aws:rds-db:*:*:dbuser:*/<db-username>"]
   - Lambda/ECS → EventBridge:
       actions: ["events:PutEvents"]
       resources: ["arn:aws:events:*:*:event-bus/<bus-name>"]

3. LAMBDA INVOCATION PERMISSIONS (inbound triggers)
   When another service points TO a Lambda, add invoke_permissions[] on the Lambda config.
   Each entry needs: statement_id (unique string), source_service (the principal), source_arn (optional).

   Connection rules (source → Lambda):
   - EventBridge → Lambda:
       invoke_permissions:
         - statement_id: "AllowEventBridgeInvoke"
           source_service: "events.amazonaws.com"
           source_arn: "arn:aws:events:<region>:<account>:rule/<rule-name>"
   - SNS → Lambda:
       invoke_permissions:
         - statement_id: "AllowSNSInvoke"
           source_service: "sns.amazonaws.com"
           source_arn: "arn:aws:sns:*:*:<topic-name>"
   - API Gateway → Lambda:
       invoke_permissions:
         - statement_id: "AllowAPIGatewayInvoke"
           source_service: "apigateway.amazonaws.com"
   Use * for account/region fields that aren't known at design time.

4. RESOURCE-BASED POLICIES (on the target)
   Add these only when the target service supports resource policies:

   - CloudFront → S3:  add bucket_policy to the S3 resource:
       bucket_policy:
         statements:
           - Sid: "AllowCloudFrontOAC"
             Effect: "Allow"
             Principal:
               Service: "cloudfront.amazonaws.com"
             Action: "s3:GetObject"
             Resource: "arn:aws:s3:::<bucket-name>/*"

   - SNS → SQS:  add queue_policy to the SQS resource:
       queue_policy:
         statements:
           - Sid: "AllowSNSPublish"
             Effect: "Allow"
             Principal:
               Service: "sns.amazonaws.com"
             Action: "sqs:SendMessage"
             Resource: "arn:aws:sqs:*:*:<queue-name>"

   - EventBridge → SNS:  add topic_policy to the SNS resource:
       topic_policy:
         statements:
           - Sid: "AllowEventBridge"
             Effect: "Allow"
             Principal:
               Service: "events.amazonaws.com"
             Action: "sns:Publish"
             Resource: "arn:aws:sns:*:*:<topic-name>"

════════════════════════════════════════════════════════════════════════════════

════════════════════════════════════════════════════════════════════════════════
FILLING IN MISSING CONFIG (CRITICAL)
════════════════════════════════════════════════════════════════════════════════

When a resource has missing or empty properties you MUST supply sensible
defaults. Do NOT leave required Terraform fields blank — that causes apply to
fail. Use the parent VPC CIDR and region to derive appropriate values.

VPC:
  cidr_block → "10.0.0.0/16" (default)
  enable_dns_hostnames → true
  enable_dns_support   → true

Subnet — derive CIDR from the parent VPC cidr_block automatically:
  Public subnets:  10.x.1.0/24, 10.x.2.0/24, 10.x.3.0/24 …
  Private subnets: 10.x.10.0/24, 10.x.11.0/24, 10.x.12.0/24 …
  (where x = second octet of the VPC CIDR, e.g. VPC=10.0.0.0/16 → x=0)
  availability_zone → cycle through the region's AZs (e.g. us-west-2a, us-west-2b, us-west-2c)
  map_public_ip_on_launch → true for public, false for private

EC2:
  instance_type → "t3.micro"
  ami (by region):
    us-east-1:      "ami-0c7217cdde317cfec"
    us-east-2:      "ami-05fb0b8c1424f266b"
    us-west-1:      "ami-0ce2cb35386fc22e9"
    us-west-2:      "ami-008fe2fc65df48dac"
    ap-south-1:     "ami-0f5ee92e2d63afc18"
    ap-southeast-1: "ami-0df7a207adb9748c7"
    ap-southeast-2: "ami-0310483fb2b488153"
    ap-northeast-1: "ami-0d52744d6551d851e"
    eu-west-1:      "ami-0d71ea30463e0ff49"
    eu-west-2:      "ami-0eb260c4d5475b901"
    eu-central-1:   "ami-0faab6bdbac9486fb"
    sa-east-1:      "ami-037eba0eb03f95689"

RDS:
  engine          → "postgres"
  engine_version  → "8.0" (mysql) / "15.3" (postgres)
  instance_class  → "db.t3.micro"
  allocated_storage → 20
  db_name         → resource name with hyphens replaced by underscores

Lambda:
  runtime      → "python3.12"
  handler      → "index.handler"
  memory_size  → 128
  timeout      → 30

ECS:
  cpu          → 256
  memory       → 512
  container_port → 80

ElastiCache:
  node_type       → "cache.t3.micro"
  engine          → "redis"
  num_cache_nodes → 1

DynamoDB:
  billing_mode   → "PAY_PER_REQUEST"
  hash_key       → "id"
  hash_key_type  → "S"

S3:
  versioning          → false
  public_access_block → true

ALB:
  internal → false (public-facing) / true (if only connected to private resources)

════════════════════════════════════════════════════════════════════════════════

IMPORTANT:
- Respond with ONLY a JSON object.  No markdown fences, no explanation.
- Every resource on the canvas that maps to a supported module type MUST appear.
- Every CONNECTION must produce the appropriate access configs above.
- Sanitize resource names to be DNS-safe (lowercase, hyphens, no spaces).
- Fill in ALL missing required fields using the defaults above — never leave them blank.
- NEVER use IAM wildcard actions (*).  Always specify exact actions.
- Use the resource NAME (not canvas ID) in ARN patterns.
"""


def _build_prompt(payload: dict) -> str:
    """Build the user prompt from the deploy payload."""
    project = payload.get("project", "my-infra")
    region = payload.get("region", "us-east-1")
    resources = payload.get("resources", [])
    connections = payload.get("connections", [])
    existing = payload.get("existing_resources", [])

    supported = sorted(SUPPORTED_MODULES)

    existing_section = ""
    if existing:
        existing_section = f"""
Already-deployed resources (live in AWS — use their names/CIDRs as context when filling defaults):
{json.dumps(existing, indent=2)}
"""

    return f"""\
Classify the following architecture canvas into per-module-type YAML configs.

Project: {project}
Region: {region}
Supported module types: {', '.join(supported)}
{existing_section}
Resource hierarchy (nested = parent-child relationship):
{json.dumps(resources, indent=2)}

Connections between resources (source → target):
{json.dumps(connections, indent=2)}

Return ONLY a JSON object: {{ "module_type": {{ "resource-name": {{ config }} }} }}"""


def generate_resource_yamls(
    payload: dict,
    anthropic_api_key: str,
    model: str = "claude-opus-4-6",
) -> dict[str, dict]:
    """
    Call Claude to classify canvas resources into per-module-type YAML configs.

    Returns: dict[str, dict]
        e.g. {
            "vpc": {"my-vpc": {"cidr_block": "10.0.0.0/16"}},
            "ec2": {"web-server": {"instance_type": "t3.micro", ...}}
        }
    """
    import anthropic

    client = anthropic.Anthropic(api_key=anthropic_api_key)

    user_prompt = _build_prompt(payload)

    logger.info("Calling Claude (%s) to classify resources into YAML configs...", model)

    message = client.messages.create(
        model=model,
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    response_text = message.content[0].text.strip()

    # Strip markdown fences if Claude wrapped the response
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        response_text = "\n".join(lines)

    try:
        resource_map = json.loads(response_text)
    except json.JSONDecodeError as e:
        logger.error("Claude returned invalid JSON: %s", response_text[:500])
        raise ValueError(f"Failed to parse Claude response as JSON: {e}")

    if not isinstance(resource_map, dict):
        raise ValueError("Claude response must be a JSON object of module_type → resources")

    # Validate module types
    for module_type in list(resource_map.keys()):
        if module_type not in SUPPORTED_MODULES:
            logger.warning("Unsupported module type '%s' — skipping", module_type)
            del resource_map[module_type]

    total = sum(len(v) for v in resource_map.values())
    logger.info(
        "Classified %d resources across %d module types: %s",
        total, len(resource_map), list(resource_map.keys()),
    )
    return resource_map


# ── Destroy analysis ────────────────────────────────────────────────────────

DESTROY_SYSTEM_PROMPT = """\
You are an expert AWS infrastructure engineer.  You receive:
1. ALL currently deployed resource configs (YAML) — the "green" / live resources.
2. The actual Terraform module code (.tf files) that manages these resources.
3. A request to DESTROY a specific resource.

Your job: read the Terraform code to understand the REAL dependency graph,
then figure out what needs to change across ALL modules to safely remove that
resource without leaving dangling references.

UNDERSTANDING DEPENDENCIES FROM THE TERRAFORM CODE:
- Read each module's main.tf to see how resources reference each other via
  `lookup(var.xxx_ids, each.value.xxx_name, null)` patterns.
- These cross-references tell you the real dependency chain.
- For example, if route-table/main.tf does
  `nat_gateway_id = lookup(var.nat_gateway_ids, ...)` and a route table config
  has `gateway_name: "my-nat-gw"`, then that route table DEPENDS on that NAT GW.

UNDERSTANDING TERRAGRUNT APPLY ORDER:
- `terragrunt run-all apply` runs modules in CREATION order (dependencies first).
- This means parent modules (vpc, subnet, security-group) run BEFORE child
  modules (ec2, nat-gateway, route-table).
- When you remove a resource from resources.yaml and run apply, Terraform
  destroys it. But if a PARENT module tries to destroy a resource that a CHILD
  module still references, AWS will BLOCK the delete (timeout for ~10 minutes).
- Therefore: ONLY remove the exact resource the user asked to destroy from its
  own module. Do NOT cascade-remove supporting resources from parent modules.
  For example: destroying an EC2 → only update ec2/resources.yaml.
  Do NOT also remove its security group from security-group/resources.yaml.

BLOCKING RULES:
If the user asks to destroy a resource but other deployed resources DEPEND on it
(checked by reading the Terraform code and config YAML), you MUST return a
BLOCKED response telling the user which resources to delete first.

When BLOCKED, return:
{
  "blocked": true,
  "explanation": "Cannot destroy <resource>. You must first delete:\\n- <resource-type>/<resource-name> (reason)\\n- ...",
  "modules_to_update": {},
  "destroy_order": []
}

When NOT blocked (safe to destroy), return:
{
  "blocked": false,
  "modules_to_update": {
    "<module-type>": { <remaining resources after removal> },
    ...
  },
  "destroy_order": ["module-type-1", "module-type-2", ...],
  "explanation": "brief explanation of what will be destroyed and why"
}

IMPORTANT:
- ONLY remove the exact resource the user asked to destroy.  Do NOT cascade-remove
  supporting resources (security groups, subnets, route tables, etc.) that the
  target resource references.  Parent modules run BEFORE child modules during
  apply, so cascade-removing causes ordering failures (10+ minute hangs).
- Only include module types that NEED changes.
- The "destroy_order" lists modules in the order they should be applied.
- Respond with ONLY JSON.  No markdown fences, no extra text.
"""


def analyze_destroy(
    resource_type: str,
    resource_name: str,
    all_resources: dict[str, dict],
    anthropic_api_key: str,
    model: str = "claude-opus-4-6",
    frontend_deployed: list[dict] | None = None,
    terraform_code: dict[str, dict[str, str]] | None = None,
) -> dict:
    """
    Ask Claude to analyze what needs to change to safely destroy a resource.

    Args:
        resource_type: Module type (e.g. "internet-gateway")
        resource_name: Resource name (e.g. "spoke-vpc-igw")
        all_resources: { module_type: { resource_name: config } } — all deployed resources
        anthropic_api_key: API key
        model: Claude model to use
        frontend_deployed: list of { resource_type, resource_name } from frontend canvas
        terraform_code: { module_type: { "main.tf": "...", ... } } — actual TF code

    Returns:
        {
            "blocked": bool,
            "modules_to_update": { module_type: { remaining resources } },
            "destroy_order": [module_type, ...],
            "explanation": "..."
        }
    """
    import anthropic

    client = anthropic.Anthropic(api_key=anthropic_api_key)

    # Build context about what the frontend knows is deployed
    frontend_context = ""
    if frontend_deployed:
        frontend_context = (
            "\n\nThe user's canvas shows these deployed (green/live) resources:\n"
            + json.dumps(frontend_deployed, indent=2)
            + "\n\nUse this to cross-check which resources are still live."
        )

    # Build Terraform code section
    tf_code_section = ""
    if terraform_code:
        tf_code_section = "\n\n═══ TERRAFORM MODULE CODE (.tf files) ═══\n"
        for mod_type, files in terraform_code.items():
            tf_code_section += f"\n── Module: {mod_type} ──\n"
            for filename, content in files.items():
                tf_code_section += f"\n### {filename}\n```hcl\n{content}\n```\n"

    user_prompt = f"""\
I want to DESTROY the resource "{resource_name}" from the "{resource_type}" module.

═══ ALL CURRENTLY DEPLOYED RESOURCES (green/live) ═══
{json.dumps(all_resources, indent=2)}{frontend_context}
{tf_code_section}
Read the Terraform code above to understand the real dependency graph between
modules.  Then analyze whether it is safe to destroy "{resource_type}/{resource_name}".

If other deployed resources depend on "{resource_name}" and would break or block
the destroy, return a BLOCKED response telling the user exactly which resources
to delete first and why.

If safe to proceed, return the updated resources.yaml content for ONLY the
target module ("{resource_type}").  Do NOT cascade-remove from parent modules."""

    logger.info("Asking Claude (%s) to analyze destroy: %s/%s", model, resource_type, resource_name)

    message = client.messages.create(
        model=model,
        max_tokens=4096,
        system=DESTROY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    response_text = message.content[0].text.strip()

    if response_text.startswith("```"):
        lines = response_text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        response_text = "\n".join(lines)

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError as e:
        logger.error("Claude destroy analysis returned invalid JSON: %s", response_text[:500])
        raise ValueError(f"Failed to parse Claude destroy response: {e}")

    logger.info(
        "Destroy analysis: %d modules affected, order=%s, explanation=%s",
        len(result.get("modules_to_update", {})),
        result.get("destroy_order", []),
        result.get("explanation", "")[:200],
    )
    return result


# ── Terragrunt HCL templates ────────────────────────────────────────────────
# Dependencies each module type needs from other modules' outputs.

MODULE_DEPENDENCIES: dict[str, list[dict[str, str]]] = {
    "vpc":              [],
    "subnet":           [{"name": "vpc", "output": "vpc_ids"}],
    "ec2":              [{"name": "subnet", "output": "subnet_ids"},
                         {"name": "security-group", "output": "security_group_ids"}],
    "security-group":   [{"name": "vpc", "output": "vpc_ids"}],
    "s3":               [],
    "rds":              [{"name": "subnet", "output": "subnet_ids"},
                         {"name": "security-group", "output": "security_group_ids"}],
    "lambda":           [{"name": "subnet", "output": "subnet_ids"},
                         {"name": "security-group", "output": "security_group_ids"}],
    "alb":              [{"name": "vpc", "output": "vpc_ids"},
                         {"name": "subnet", "output": "subnet_ids"},
                         {"name": "security-group", "output": "security_group_ids"},
                         {"name": "ec2", "output": "instance_ids"}],
    "internet-gateway": [{"name": "vpc", "output": "vpc_ids"}],
    "nat-gateway":      [{"name": "subnet", "output": "subnet_ids"}],
    "eventbridge":      [],
    "sqs":              [],
    "sns":              [],
    "dynamodb":         [],
    "route53":          [{"name": "vpc", "output": "vpc_ids"}],
    "ecs":              [],
    "cloudfront":       [],
    "elasticache":      [{"name": "subnet", "output": "subnet_ids"},
                         {"name": "security-group", "output": "security_group_ids"}],
}


def build_terragrunt_hcl(module_type: str, modules_in_deploy: set[str]) -> str:
    """Generate a terragrunt.hcl for a given module type."""
    deps = MODULE_DEPENDENCIES.get(module_type, [])
    # Only include dependencies for modules that actually exist in this deploy
    active_deps = [d for d in deps if d["name"] in modules_in_deploy]

    lines = [
        'include "root" {',
        "  path = find_in_parent_folders()",
        "}",
        "",
        "terraform {",
        f'  source = "${{get_repo_root()}}/modules//{module_type}"',
        "}",
        "",
        "locals {",
        '  config = yamldecode(file("resources.yaml"))',
        "}",
        "",
    ]

    # Dependency blocks
    for dep in active_deps:
        lines += [
            f'dependency "{dep["name"]}" {{',
            f'  config_path = "../{dep["name"]}"',
            "  mock_outputs = {",
            f'    {dep["output"]} = {{}}',
            "  }",
            "}",
            "",
        ]

    # Inputs
    lines += ["inputs = {", "  resources = local.config", f'  project   = "{module_type}"']

    for dep in active_deps:
        lines.append(f'  {dep["output"]} = dependency.{dep["name"]}.outputs.{dep["output"]}')

    lines += ["}"]

    return "\n".join(lines) + "\n"


def build_project_yaml(project: str, region: str) -> str:
    """Generate the project.yaml config read by root terragrunt.hcl."""
    return yaml.dump({"project": project, "region": region}, default_flow_style=False)


def resources_to_yaml(resources: dict) -> str:
    """Convert a resource config dict to YAML string."""
    return yaml.dump(resources, default_flow_style=False, sort_keys=False)
