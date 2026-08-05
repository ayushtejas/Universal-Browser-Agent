#!/usr/bin/env bash
# Build, push to ECR, and run as an ECS task.
#
# Prerequisites:
#   aws configure          (or export AWS_PROFILE=...)
#   Create the secrets in AWS Secrets Manager:
#     aws secretsmanager create-secret --name ipr-scraper/mongodb-uri   --secret-string "$MONGODB_URI"
#     aws secretsmanager create-secret --name ipr-scraper/openai-api-key --secret-string "$OPENAI_API_KEY"
#
# Usage:
#   ./deploy/deploy.sh              # build + push + run-task
#   ./deploy/deploy.sh build        # build + push only
#   ./deploy/deploy.sh run          # run-task only (image already pushed)

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REPO="ipr-scraper"
IMAGE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO}:latest"
CLUSTER="${ECS_CLUSTER:-default}"
TASK_FAMILY="ipr-scraper"

# -- ECR login + repo --
ensure_repo() {
    aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" 2>/dev/null \
        || aws ecr create-repository --repository-name "$REPO" --region "$REGION"
    aws ecr get-login-password --region "$REGION" \
        | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
}

# -- Build + push --
build() {
    echo "▸ building $IMAGE"
    docker build --platform linux/amd64 -t "$REPO" .
    docker tag "$REPO" "$IMAGE"
    ensure_repo
    echo "▸ pushing"
    docker push "$IMAGE"
    echo "✓ pushed $IMAGE"
}

# -- Register task def + run --
run_task() {
    # Substitute ACCOUNT_ID in the task def template
    TASK_DEF=$(sed "s/ACCOUNT_ID/${ACCOUNT_ID}/g" deploy/ecs-task-def.json)

    echo "▸ registering task definition"
    aws ecs register-task-definition \
        --cli-input-json "$TASK_DEF" \
        --region "$REGION" \
        --query 'taskDefinition.taskDefinitionArn' \
        --output text

    echo "▸ running task on cluster=$CLUSTER"
    # Grab the first public subnet + default SG — adjust for your VPC.
    SUBNET=$(aws ec2 describe-subnets \
        --filters "Name=default-for-az,Values=true" \
        --query 'Subnets[0].SubnetId' --output text --region "$REGION")
    SG=$(aws ec2 describe-security-groups \
        --filters "Name=group-name,Values=default" \
        --query 'SecurityGroups[0].GroupId' --output text --region "$REGION")

    TASK_ARN=$(aws ecs run-task \
        --cluster "$CLUSTER" \
        --task-definition "$TASK_FAMILY" \
        --launch-type FARGATE \
        --network-configuration "awsvpcConfiguration={subnets=[$SUBNET],securityGroups=[$SG],assignPublicIp=ENABLED}" \
        --region "$REGION" \
        --query 'tasks[0].taskArn' \
        --output text)

    echo "✓ task started: $TASK_ARN"
    echo "  logs: aws logs tail /ecs/ipr-scraper --follow --region $REGION"
}

case "${1:-all}" in
    build) build ;;
    run)   run_task ;;
    all)   build; run_task ;;
    *)     echo "usage: $0 [build|run|all]"; exit 1 ;;
esac
