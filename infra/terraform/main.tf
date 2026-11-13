terraform {
  required_version = ">= 1.5"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.0" }
    http   = { source = "hashicorp/http", version = "~> 3.4" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile != "" ? var.aws_profile : null
}

# ---- Network: default VPC + subnets, SG scoped to the caller's public IP ----
data "http" "myip" {
  count = var.allowed_cidr == "" ? 1 : 0
  url   = "https://checkip.amazonaws.com"
}

locals {
  ingress_cidr = var.allowed_cidr != "" ? var.allowed_cidr : "${chomp(data.http.myip[0].response_body)}/32"
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# A publicly-accessible RDS/Aurora instance is only reachable if it sits in a
# subnet whose route table has a route to an internet gateway. Many "default"
# VPCs (especially in shared/enterprise accounts) are customized so that only
# SOME subnets are public -- placing a DB in a private subnet gives it a public
# DNS name that never connects (silent timeout). We therefore look up each
# subnet's route table and keep only the ones with an igw- route. Override with
# var.public_subnet_ids if you already know your public subnets.
data "aws_route_table" "per_subnet" {
  for_each  = var.public_subnet_ids == null ? toset(data.aws_subnets.default.ids) : toset([])
  subnet_id = each.value
}

locals {
  detected_public_subnet_ids = [
    for sid, rt in data.aws_route_table.per_subnet : sid
    if length([for r in rt.routes : r if r.gateway_id != null && startswith(r.gateway_id, "igw-")]) > 0
  ]
  public_subnet_ids = var.public_subnet_ids != null ? var.public_subnet_ids : local.detected_public_subnet_ids
}

resource "aws_db_subnet_group" "this" {
  name       = "${var.name_prefix}-subnets"
  subnet_ids = local.public_subnet_ids
  tags       = { project = "db-branching-benchmark" }

  lifecycle {
    precondition {
      condition     = length(local.public_subnet_ids) >= 2
      error_message = "Need >=2 public subnets (igw-routed, distinct AZs) for an RDS subnet group. Found ${length(local.public_subnet_ids)}. Set var.public_subnet_ids explicitly, or run in a region/VPC with public subnets."
    }
  }
}

resource "aws_security_group" "db" {
  name        = "${var.name_prefix}-sg"
  description = "db-branching-benchmark client access"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "postgres from caller"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [local.ingress_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { project = "db-branching-benchmark" }
}

resource "random_password" "master" {
  length  = 20
  special = false # keep it URL/connection-string safe
}

# ---- RDS PostgreSQL (single instance) ----
resource "aws_db_instance" "rds_postgres" {
  count                       = var.enable_rds_postgres ? 1 : 0
  identifier                  = "${var.name_prefix}-rds-pg"
  engine                      = "postgres"
  engine_version              = var.postgres_engine_version
  instance_class              = var.rds_instance_class
  allocated_storage           = var.rds_allocated_storage
  storage_type                = "gp3"
  username                    = var.master_username
  password                    = random_password.master.result
  db_subnet_group_name        = aws_db_subnet_group.this.name
  vpc_security_group_ids      = [aws_security_group.db.id]
  publicly_accessible         = true
  skip_final_snapshot         = true
  backup_retention_period     = 1
  apply_immediately           = true
  tags                        = { project = "db-branching-benchmark" }
}

# ---- Aurora PostgreSQL cluster (fast clone = storage-layer CoW) ----
resource "aws_rds_cluster" "aurora_pg" {
  count                   = var.enable_aurora_postgres ? 1 : 0
  cluster_identifier      = "${var.name_prefix}-aurora-pg"
  engine                  = "aurora-postgresql"
  engine_version          = var.aurora_postgres_engine_version
  master_username         = var.master_username
  master_password         = random_password.master.result
  db_subnet_group_name    = aws_db_subnet_group.this.name
  vpc_security_group_ids  = [aws_security_group.db.id]
  skip_final_snapshot     = true
  backup_retention_period = 1
  apply_immediately       = true
  tags                    = { project = "db-branching-benchmark" }
}

resource "aws_rds_cluster_instance" "aurora_pg" {
  count                = var.enable_aurora_postgres ? 1 : 0
  identifier           = "${var.name_prefix}-aurora-pg-1"
  cluster_identifier   = aws_rds_cluster.aurora_pg[0].id
  engine               = aws_rds_cluster.aurora_pg[0].engine
  engine_version       = aws_rds_cluster.aurora_pg[0].engine_version
  instance_class       = var.aurora_instance_class
  db_subnet_group_name = aws_db_subnet_group.this.name
  publicly_accessible  = true
  tags                 = { project = "db-branching-benchmark" }
}
