#!/bin/bash
set -e

# Configuration
AWS_REGION="ap-south-1"
AWS_ACCOUNT_ID="545581984494"
ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
ECR_REPO="ezeehealth-unified-backend"
IMAGE_TAG=$(date +%Y%m%d%H%M%S)

echo "Building and pushing ezeehealth-unified backend..."
echo "   Tag: $IMAGE_TAG"

# Login to ECR
echo "Logging into ECR..."
aws ecr get-login-password --region $AWS_REGION | \
  docker login --username AWS --password-stdin $ECR_REGISTRY

# Build
echo "Building image..."
docker build \
  --platform linux/amd64 \
  -t $ECR_REGISTRY/$ECR_REPO:$IMAGE_TAG \
  -t $ECR_REGISTRY/$ECR_REPO:latest \
  .

# Push
echo "Pushing to ECR..."
docker push $ECR_REGISTRY/$ECR_REPO:$IMAGE_TAG
docker push $ECR_REGISTRY/$ECR_REPO:latest

echo ""
echo "Done! Image pushed:"
echo "   $ECR_REGISTRY/$ECR_REPO:$IMAGE_TAG"
echo "   $ECR_REGISTRY/$ECR_REPO:latest"
echo ""
echo "Now SSH to EC2 and run:"
echo "   cd ~/ezeehealth-unified && ./deploy.sh"
