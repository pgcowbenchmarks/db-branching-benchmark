output "region" {
  value = var.region
}

output "master_username" {
  value = var.master_username
}

output "master_password" {
  value     = random_password.master.result
  sensitive = true
}

output "ingress_cidr" {
  value = local.ingress_cidr
}

output "rds_postgres_host" {
  value = var.enable_rds_postgres ? aws_db_instance.rds_postgres[0].address : ""
}

output "aurora_postgres_host" {
  value = var.enable_aurora_postgres ? aws_rds_cluster.aurora_pg[0].endpoint : ""
}

output "aurora_postgres_cluster_id" {
  value = var.enable_aurora_postgres ? aws_rds_cluster.aurora_pg[0].cluster_identifier : ""
}

output "aurora_instance_class" {
  value = var.aurora_instance_class
}
