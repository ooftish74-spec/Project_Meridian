#!/bin/bash
# =================================================================
# Project Meridian — Build & Push Docker Image to AWS ECR
# =================================================================
set -e

# Configuration
AWS_REGION=${AWS_REGION:-"ap-northeast-2"}
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "ACCOUNT_ID")
REPO_NAME="project-meridian"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
GIT_HASH=$(git rev-parse --short HEAD 2>/dev/null || echo "latest")

ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${REPO_NAME}"

echo "=========================================================="
echo " 🐳 Building Project Meridian Immutable Docker Image"
echo "   AWS Region : ${AWS_REGION}"
echo "   ECR URI    : ${ECR_URI}"
echo "   Tag        : ${TIMESTAMP}_${GIT_HASH}"
echo "=========================================================="

# 1. AWS ECR Login
echo "🔑 Logging into AWS ECR..."
aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${ECR_URI}

# 2. Build Docker Image
echo "🔨 Building Docker Image..."
docker build -t ${REPO_NAME}:latest -t ${REPO_NAME}:${GIT_HASH} -t ${REPO_NAME}:${TIMESTAMP} .

# 3. Tag Image for ECR
docker tag ${REPO_NAME}:latest ${ECR_URI}:latest
docker tag ${REPO_NAME}:${GIT_HASH} ${ECR_URI}:${GIT_HASH}
docker tag ${REPO_NAME}:${TIMESTAMP} ${ECR_URI}:${TIMESTAMP}

# 4. Push to ECR
echo "🚀 Pushing image to AWS ECR..."
docker push ${ECR_URI}:latest
docker push ${ECR_URI}:${GIT_HASH}
docker push ${ECR_URI}:${TIMESTAMP}

echo "=========================================================="
echo " ✅ SUCCESS! Project Meridian Image Pushed to ECR:"
echo "    Image URI: ${ECR_URI}:latest"
echo "    Version  : ${ECR_URI}:${GIT_HASH}"
echo "=========================================================="
