# DevOps AI Backend

AWS Lambda that receives architecture JSON from the frontend, generates Terraform/Terragrunt code using Claude, and pushes it to the `devops-infra-live` repo.

## Architecture

```
Frontend (POST /api/deploy)
    │
    ▼
API Gateway → Lambda (this code)
    │
    ├─ 1. Call Claude API → generate .tf files
    │
    └─ 2. Push files to devops-infra-live repo
         on branch: feature/<project-name>
         + create Pull Request
```

## Setup

### Prerequisites
- Python 3.12+
- AWS SAM CLI (for deployment)
- GitHub Personal Access Token (with `repo` scope)
- Anthropic API key

### Environment Variables

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude |
| `GITHUB_TOKEN` | GitHub PAT with `repo` scope |
| `GITHUB_REPO` | Target repo, e.g. `your-org/devops-infra-live` |

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set env vars
export ANTHROPIC_API_KEY="sk-ant-..."
export GITHUB_TOKEN="ghp_..."
export GITHUB_REPO="your-org/devops-infra-live"

# Run locally with SAM
sam local start-api

# Or test the function directly
sam local invoke GenerateAndPushFunction -e test/event.json
```

### Deploy to AWS

```bash
sam build
sam deploy --guided
```

After deployment, set `VITE_DEPLOY_URL` in the frontend to the API Gateway URL.

### GitHub Actions Deployment

This repo includes [deploy.yml](/C:/Users/Aditya%20Shashank/Documents/Devops-repos/Devops-backend/.github/workflows/deploy.yml), which:

- validates and builds the SAM app on pull requests to `main`
- deploys the Lambda stack automatically on pushes to `main`
- uses GitHub OIDC to assume an AWS role instead of long-lived AWS keys

Required GitHub configuration:

| Type | Name | Purpose |
|---|---|---|
| Secret | `AWS_ROLE_ARN` | IAM role ARN that GitHub Actions will assume |
| Secret | `AWS_REGION` | AWS region for the SAM deploy |
| Variable | `SAM_STACK_NAME` | CloudFormation stack name, defaults to `devops-backend` |

The AWS role trusted by GitHub Actions must allow deploying CloudFormation, Lambda, API Gateway, IAM, and reading your referenced Secrets Manager values.

## Test Event

```json
{
  "requestContext": { "http": { "method": "POST" } },
  "rawPath": "/api/deploy",
  "body": "{\"project\":\"test\",\"region\":\"us-east-1\",\"resources\":[{\"id\":\"node_0\",\"resourceType\":\"vpc\",\"name\":\"My VPC\",\"properties\":{\"cidrBlock\":\"10.0.0.0/16\"},\"children\":[{\"id\":\"node_1\",\"resourceType\":\"subnet-public\",\"name\":\"Public Subnet\",\"properties\":{\"cidrBlock\":\"10.0.1.0/24\"},\"children\":[]}]}],\"connections\":[]}"
}
```
