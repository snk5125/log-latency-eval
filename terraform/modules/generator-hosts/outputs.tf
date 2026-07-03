########################################################################
# module: generator-hosts — outputs
########################################################################

output "instance_ids" {
  description = "Map of generator logical name (e.g. lin-vec, win-ce) -> EC2 instance ID. The orchestrator uses these to start/stop generators via SSM per run."
  value       = { for k, inst in aws_instance.generator : k => inst.id }
}

output "private_ips" {
  description = "Map of generator logical name -> private IP (diagnostics / inventory)."
  value       = { for k, inst in aws_instance.generator : k => inst.private_ip }
}
