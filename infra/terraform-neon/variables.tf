variable "neon_api_key" {
  description = "Neon API key. Leave null to read it from the NEON_API_KEY environment variable (recommended — keeps the secret out of files)."
  type        = string
  default     = null
  sensitive   = true
}

variable "project_name" {
  description = "Name of the Neon project to create."
  type        = string
  default     = "db-branching-benchmark"
}

variable "region_id" {
  description = "Neon region. Co-locate with the AWS fleet (us-west-2) so cross-engine latency is comparable."
  type        = string
  default     = "aws-us-west-2"
}

variable "pg_version" {
  description = "Postgres major version for the Neon project. 17+ recommended (18 where available)."
  type        = number
  default     = 17
}

variable "history_retention_seconds" {
  description = "Point-in-time history window. Lower = less storage overhead for a throwaway benchmark project."
  type        = number
  default     = 3600
}
