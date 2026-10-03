# Install (pilot)

Target: one Linux server with Docker, [Dokploy](https://dokploy.com) (its Traefik terminates TLS) and a
WireGuard VPN on `10.8.0.0/24` with the server at `10.8.0.1` (e.g. [wg-easy](https://github.com/wg-easy/wg-easy)).
Matrix is reachable only from the VPN.

## 1. DNS and firewall

- `A matrix.example.com → <server public IP>`. The public record is only for Let's Encrypt (HTTP-01);
  Traefik answers 403 to anyone outside `10.8.0.0/24`.
- Open TCP 80 and 443 to the public in your provider firewall.

## 2. Prepare the host

```bash
git clone https://github.com/<you>/retinue /opt/retinue
sudo bash /opt/retinue/deploy/setup.sh matrix.example.com alice
```

The script creates `/srv/retinue` (appservice registration, router config, agent workspaces,
`secrets.env` with generated tokens), installs the Traefik middleware `vpn-only` and starts split DNS
(dnsmasq on `10.8.0.1`). It prints the environment block for the next step.

## 3. VPN clients use the split DNS

Set the WireGuard client DNS to `10.8.0.1` (wg-easy: *Admin → Config → DNS*) and re-import the
client profiles. From a VPN client: `dig matrix.example.com` → `10.8.0.1`.

## 4. Deploy the stack

Dokploy → project → *Compose* from your Git repo, compose path `./deploy/compose.yml`. Paste the
printed environment block, plus the model credential: `CLAUDE_CODE_OAUTH_TOKEN` from
`claude setup-token` (your own subscription, personal use) or `ANTHROPIC_API_KEY`. Deploy.

## 5. Create your account and close registration

```bash
docker exec -it <router container> retinue-admin register --username alice
```

Then set `MATRIX_ALLOW_REGISTRATION=false` in Dokploy and redeploy.

## 6. Talk to your agents

Element X → *Other homeserver* → `matrix.example.com` → sign in. Accept the invitations: one room
per agent plus *Протокол* (the protocol log). Write in an agent's room.

## Smoke test without a model

```bash
docker build -t retinue:local .
docker compose -f tests/e2e/compose.yml up -d && python3 tests/e2e/smoke.py
docker compose -f tests/e2e/compose.yml down -v
```
