output "project_id" {
  description = "Neon project id — feed this to the harness as engines.neon.project_id."
  value       = neon_project.bench.id
}

output "region_id" {
  value = neon_project.bench.region_id
}

output "pg_version" {
  value = neon_project.bench.pg_version
}

output "default_branch_id" {
  value = neon_project.bench.default_branch_id
}

# Pooled (PgBouncer) endpoint host — the connection path the harness uses so
# connections are recycled rather than cold-starting a backend per connect.
output "database_host_pooler" {
  value = neon_project.bench.database_host_pooler
}

output "connection_uri_pooler" {
  description = "Full pooled connection string for the project's default branch (verification/seed)."
  value       = neon_project.bench.connection_uri_pooler
  sensitive   = true
}
