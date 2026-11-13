variable "aws_profile" {
  description = "AWS CLI profile to use. Leave empty to use the default credential chain (env vars, SSO, instance role)."
  type        = string
  default     = ""
}

variable "region" {
  description = "AWS region. Keep the benchmark client co-located with the databases."
  type        = string
  default     = "us-west-2"
}

variable "name_prefix" {
  description = "Prefix for all created resources."
  type        = string
  default     = "bbench"
}

variable "allowed_cidr" {
  description = "CIDR allowed to reach the databases on 5432/3306. Empty = auto-detect the caller's public IP (/32)."
  type        = string
  default     = ""
}

variable "public_subnet_ids" {
  description = "Explicit list of public (igw-routed) subnet IDs for the DB subnet group. null = auto-detect the public subnets of the default VPC by inspecting route tables."
  type        = list(string)
  default     = null
}

variable "master_username" {
  type    = string
  default = "bench"
}

variable "rds_instance_class" {
  type    = string
  default = "db.t3.large"
}

variable "aurora_instance_class" {
  type    = string
  default = "db.r6g.large"
}

variable "rds_allocated_storage" {
  description = "GB for each single-instance RDS (gp3). Size for the largest scale you will run."
  type        = number
  default     = 150
}

variable "postgres_engine_version" {
  type    = string
  default = "16.9"
}

variable "aurora_postgres_engine_version" {
  type    = string
  default = "16.9"
}

variable "enable_rds_postgres" {
  type    = bool
  default = true
}

variable "enable_aurora_postgres" {
  type    = bool
  default = true
}
