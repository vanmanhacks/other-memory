{ config, pkgs, lib, ... }:

{
  # ─── Docker Compose Stack ───
  systemd.services.other-memory-stack = {
    description = "Other Memory — Self-Hosted Search & Crawl Stack";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" "docker.service" ];
    requires = [ "docker.service" "other-memory-secret.service" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      WorkingDirectory = "${config.users.users.vanmanhacks.home}/Operations/GHOLA-OM/other-memory";
      EnvironmentFile = "/etc/other-memory/env";
      ExecStart = "${pkgs.docker}/bin/docker compose up -d";
      ExecStop = "${pkgs.docker}/bin/docker compose down";
    };
  };

  # ─── Secret Generation ──────────────────────────────────────────────
  systemd.services.other-memory-secret = {
    description = "Generate Other Memory secrets";
    wantedBy = [ "other-memory-stack.service" ];
    before = [ "other-memory-stack.service" ];
    serviceConfig = {
      Type = "oneshot";
      User = "vanmanhacks";
      # Create /etc/other-memory as root first, then demote to user for secret generation
      ExecStartPre = [
        "+${pkgs.coreutils}/bin/mkdir -p /etc/other-memory"
      ];
      ExecStart = pkgs.writeShellScript "other-memory-secrets" ''
        set -euo pipefail

        if [ ! -f /etc/other-memory/searxng_secret ]; then
          ${pkgs.openssl}/bin/openssl rand -hex 32 > /etc/other-memory/searxng_secret
        fi

        if [ ! -f /etc/other-memory/env ]; then
          touch /etc/other-memory/env
          chmod 600 /etc/other-memory/env
        fi

        if [ ! -f /etc/other-memory/crawl4ai_token ]; then
          ${pkgs.openssl}/bin/openssl rand -hex 16 > /etc/other-memory/crawl4ai_token
        fi

        if ! grep -q "^SEARXNG_SECRET=" /etc/other-memory/env 2>/dev/null; then
          echo "SEARXNG_SECRET=$(cat /etc/other-memory/searxng_secret)" >> /etc/other-memory/env
        fi

        if ! grep -q "^CRAWL4AI_TOKEN=" /etc/other-memory/env 2>/dev/null; then
          echo "CRAWL4AI_TOKEN=$(cat /etc/other-memory/crawl4ai_token)" >> /etc/other-memory/env
        fi
      '';
    };
  };
}
