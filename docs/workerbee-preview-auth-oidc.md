# WorkerBee Preview Auth And OIDC Options

This note frames authentication options for a private WorkerBee preview. It is
not a production IAM, PAM, mTLS, or compliance claim.

## Recommended Pilot Path

Use a zero-trust access layer or OIDC-aware reverse proxy for human dashboard
access, and use OAuth 2.1 protected-resource style bearer validation for MCP
calls. Keep mTLS available for service-to-service or edge/core identity where
certificate operations are acceptable, but do not make mTLS the default human
onboarding path.

Recommended first implementation:

- Protect dashboard and admin routes with a ZTNA product or OIDC-aware reverse
  proxy.
- Keep MCP routes default-deny without a bearer token.
- Add non-secret issuer and token fixtures for positive MCP auth tests.
- Record dashboard and MCP authorization decisions in the audit ledger.
- Keep break-glass access explicit, time-bounded, and separately audited.

## Options

| Option | Best Use | Tradeoff |
| --- | --- | --- |
| ZTNA front door | Fast private preview access with device and user posture controls | Adds an external access plane that must be documented |
| Caddy `forward_auth` with an OIDC proxy | Simple edge enforcement for dashboard routes | Requires careful trusted-header allowlisting |
| oauth2-proxy | Common OIDC front end for HTTP apps | Adds cookie, callback, and session hardening work |
| Keycloak | Full local IdP for users, clients, roles, and groups | Operationally heavier than needed for a first preview |
| Authentik | Integrated identity workflows and proxy-provider support | Still a separate identity service to run and back up |
| Dex | Lightweight OIDC broker for upstream IdPs | Broker only; not a full IAM system |
| Cloud IdP | Fastest when the team already has an IdP | Data residency and tenant custody need review |

## Baseline Roles

- `platform_admin`: manages preview targets, registry references, gateway routes,
  and emergency rollback.
- `tenant_admin`: manages tenant or workspace settings inside approved preview
  boundaries.
- `workspace_user`: deploys and inspects user-scoped workloads.
- `read_only_auditor`: views status, evidence, and redacted audit exports.
- `break_glass_operator`: time-bounded recovery role with explicit audit events.

## Surface Boundaries

Dashboard and admin surfaces should be protected before broad preview access.
Status routes should either be hidden, gated, or reduced to a minimal
non-sensitive health response. MCP should require bearer validation for all
mutating tools and should return a clear unauthorized result for missing,
expired, wrong-audience, or wrong-issuer tokens.

mTLS is useful for workload, connector, gateway, or service identity. It is not
the recommended first human onboarding mechanism because certificate
distribution, revocation, and recovery flows are heavier than OIDC or ZTNA for
most pilot users.

## Immediate Gates

1. Gate or hide dashboard status surfaces before external partner access.
2. Add a positive MCP auth test path using non-secret fixture tokens.
3. Add dashboard auth boundary tests for unauthenticated, workspace-user,
   tenant-admin, platform-admin, read-only-auditor, and break-glass flows.
4. Emit redacted audit events for auth success, auth denial, role denial,
   break-glass activation, and route or admin mutations.
5. Verify generated proof packages do not include cookies, bearer values, client
   secrets, refresh tokens, private keys, or raw browser state.

## Non-Claims

- No production IAM/PAM is claimed by this note.
- No production mTLS rollout is claimed.
- No formal compliance approval is claimed.
- No production signing-key or secret-custody model is claimed.
- No public preview access should be opened until auth, audit, DNS/TLS, and
  rollback gates are green.
