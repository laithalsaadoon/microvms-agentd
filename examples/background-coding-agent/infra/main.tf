# SPDX-License-Identifier: Apache-2.0
terraform {
  required_version = ">= 1.8"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}
variable "name" {
  type    = string
  default = "background-coding-agent"
}
variable "image_arn" {
  type = string
}
variable "image_version" {
  type    = string
  default = "1.0"
}
variable "task_seconds" {
  description = "Agent time budget. The VM lives 15 minutes longer."
  type        = number
  default     = 1800
  validation {
    condition     = var.task_seconds >= 600 && var.task_seconds <= 3600
    error_message = "task_seconds must be between 600 and 3600."
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  trust = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Principal = { Service = "lambda.amazonaws.com" },
    Action    = ["sts:AssumeRole", "sts:TagSession"],
    Condition = { StringEquals = { "aws:SourceAccount" = local.account } }
  }] })
}

# Job records. The CLI writes them; the workflow updates them as it progresses.
resource "aws_dynamodb_table" "jobs" {
  name         = "${var.name}-jobs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"
  attribute {
    name = "id"
    type = "S"
  }
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
  point_in_time_recovery {
    enabled = true
  }
}

# Set the value yourself; Terraform never sees the token.
resource "aws_secretsmanager_secret" "github" {
  name_prefix             = "${var.name}-github-"
  recovery_window_in_days = 0
}

# Inputs and agent output, kept 30 days like the job records.
resource "aws_s3_bucket" "jobs" {
  bucket_prefix = "${var.name}-"
  force_destroy = true
}
resource "aws_s3_bucket_public_access_block" "jobs" {
  bucket                  = aws_s3_bucket.jobs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_lifecycle_configuration" "jobs" {
  bucket = aws_s3_bucket.jobs.id
  rule {
    id     = "expire-jobs"
    status = "Enabled"
    filter {}
    expiration {
      days = 30
    }
  }
}

resource "aws_iam_role" "worker" {
  name_prefix        = "${var.name}-worker-"
  assume_role_policy = local.trust
}
resource "aws_iam_role_policy_attachment" "durable" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicDurableExecutionRolePolicy"
}
resource "aws_iam_role_policy" "worker" {
  role = aws_iam_role.worker.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["lambda:RunMicrovm", "lambda:GetMicrovm", "lambda:TerminateMicrovm",
    "lambda:CreateMicrovmAuthToken", "lambda:GetMicrovmImage", "lambda:GetMicrovmImageVersion"], Resource = "*" },
    { Effect = "Allow", Action = "iam:PassRole", Resource = aws_iam_role.guest.arn },
    { Effect = "Allow", Action = "lambda:PassNetworkConnector",
    Resource = "arn:aws:lambda:${var.region}:aws:network-connector:aws-network-connector:*" },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${aws_s3_bucket.jobs.arn}/*" },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.jobs.arn },
    { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = aws_secretsmanager_secret.github.arn },
    # The agent's Bedrock bearer token is presigned with this role's credentials.
    { Effect = "Allow", Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
    "bedrock:CallWithBearerToken"], Resource = "*" }
  ] })
}

# The guest can read its role from metadata, so it gets only log delivery.
resource "aws_iam_role" "guest" {
  name_prefix        = "${var.name}-guest-"
  assume_role_policy = local.trust
}
resource "aws_iam_role_policy" "guest" {
  role = aws_iam_role.guest.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
    Resource = "arn:aws:logs:${var.region}:${local.account}:log-group:/aws/lambda-microvms/*"
  }] })
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = 14
}

resource "aws_lambda_function" "worker" {
  function_name    = var.name
  role             = aws_iam_role.worker.arn
  handler          = "handler.handler"
  runtime          = "python3.13"
  architectures    = ["x86_64"]
  filename         = "${path.module}/../.agent/function.zip"
  source_code_hash = try(filebase64sha256("${path.module}/../.agent/function.zip"), null)
  timeout          = 900
  memory_size      = 2048
  publish          = true
  durable_config {
    execution_timeout = var.task_seconds + 3600
    retention_period  = 14
  }
  environment {
    variables = {
      BUCKET         = aws_s3_bucket.jobs.id
      JOBS_TABLE     = aws_dynamodb_table.jobs.name
      GITHUB_SECRET  = aws_secretsmanager_secret.github.arn
      IMAGE_ARN      = var.image_arn
      IMAGE_VERSION  = var.image_version
      GUEST_ROLE_ARN = aws_iam_role.guest.arn
      TASK_SECONDS   = tostring(var.task_seconds)
    }
  }
  depends_on = [aws_iam_role_policy.worker, aws_iam_role_policy_attachment.durable, aws_cloudwatch_log_group.worker]
}

output "config" {
  value = {
    region        = var.region
    bucket        = aws_s3_bucket.jobs.id
    table         = aws_dynamodb_table.jobs.name
    function_arn  = aws_lambda_function.worker.qualified_arn
    github_secret = aws_secretsmanager_secret.github.arn
  }
}
