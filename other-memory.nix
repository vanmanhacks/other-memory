{ config, pkgs, lib, ... }:

let
  # Path to this other-memory checkout — adjust to your layout.
  omDir = "/opt/other-memory";
in
{
  # ─── Docker Compose Stack ───
  systemd.services.other-memory-stack = {
    description = "Other Memory — Self-Hosted Search & Crawl Stack";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" "docker.service" "other-memory-secret.service" ];
    requires = [ "docker.service" "other-memory-secret.service" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      WorkingDirectory = omDir;
      ExecStart = "${pkgs.docker}/bin/docker compose up -d";
      ExecStop = "${pkgs.docker}/bin/docker compose down";
    };
  };

  # ─── Secret Generation (.env) ──────────────────────────────────────
  # Generates the gitignored .env (SEARXNG_SECRET, REDIS_PASSWORD) that
  # docker-compose.yml consumes via env_file.
  systemd.services.other-memory-secret = {
    description = "Generate Other Memory secrets (.env)";
    wantedBy = [ "other-memory-stack.service" ];
    before = [ "other-memory-stack.service" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = pkgs.writeShellScript "other-memory-secrets" ''
        set -euo pipefail
        env_file="${omDir}/.env"
        touch "$env_file"
        chmod 600 "$env_file"
        if ! grep -q '^SEARXNG_SECRET=' "$env_file"; then
          echo "SEARXNG_SECRET=$(${pkgs.openssl}/bin/openssl rand -hex 32)" >> "$env_file"
        fi
        if ! grep -q '^REDIS_PASSWORD=' "$env_file"; then
          echo "REDIS_PASSWORD=$(${pkgs.openssl}/bin/openssl rand -hex 16)" >> "$env_file"
        fi
      '';
    };
  };
}
