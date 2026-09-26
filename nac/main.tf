# Cisco Network-as-Code for the six Catalyst 8000v routers of c8000v-dmvpn-lab (hubs East/Central/West, cust1-3).
# Module 0.1.0 / provider 0.15.0 over RESTCONF — provider 1.0's NETCONF lock is unusable on these images. Credentials
# from the environment: ../lab.sh nac sets IOSXE_USERNAME / IOSXE_PASSWORD (admin/admin), and saves after an apply.
module "iosxe" {
  source  = "netascode/nac-iosxe/iosxe"
  version = "0.1.0"

  yaml_directories = ["data"]
  save_config      = false          # lab.sh nac apply saves via the cisco-ia:save-config RPC
  write_model_file = "rendered-model.yaml"
}
