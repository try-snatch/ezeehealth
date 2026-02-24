#!/bin/bash
set -e

AWS_REGION="ap-south-1"
AWS_ACCOUNT_ID="545581984494"
ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
ECR_REPO="ezeehealth-unified-backend"

echo "Deploying ezeehealth-unified..."

# Login to ECR
echo "Logging into ECR..."
aws ecr get-login-password --region $AWS_REGION | \
  docker login --username AWS --password-stdin $ECR_REGISTRY

# Pull latest image
echo "Pulling latest image..."
docker pull $ECR_REGISTRY/$ECR_REPO:latest

# Restart services
echo "Restarting services..."
docker compose -f docker-compose-prod.yml down
docker compose -f docker-compose-prod.yml up -d

# Run migrations
echo "Running migrations..."
docker compose -f docker-compose-prod.yml exec backend python manage.py migrate

echo ""
echo "Deployment complete!"
echo "Check status: docker compose -f docker-compose-prod.yml ps"
echo "View logs:    docker compose -f docker-compose-prod.yml logs -f backend"
