# Security Policy

## Reporting a vulnerability

Please report security issues **privately**, through GitHub's private
vulnerability reporting: open the
[Security tab](https://github.com/Stefan-Schmidbauer/caldav-mcp/security)
of this repository and use **Report a vulnerability**. That opens a private
advisory visible only to you and the maintainer.

Please do not open a public issue for a vulnerability, and do not include a
working password or bearer token in the report — describe the setup instead.

This is a spare-time project maintained by one person. Expect an initial reply
within about a week; there is no guaranteed fix window.

## Supported versions

Only the latest commit on `main` is supported. There are no backports to tags.

## Scope

This server holds the credentials of a CalDAV account and exposes tools that act
with them. The [Security model](README.md#security-model) section of the README
describes what that grants and which properties are *not* claimed. Reports about
the following are expected and useful:

- a way to make the server send the credentials to a host other than the
  configured CalDAV server — for example an event URI or calendar that passes
  validation but points elsewhere
- a way to reach a calendar the account does not list, or a task list through
  an event URI
- a way to pass the `MCP_AUTH_TOKEN` bearer check without the token
- the CalDAV password or the bearer token leaking into logs, tool output, or an
  error message

Known and documented, so **not** a finding on their own — see the README:

- the CalDAV credentials grant access to the whole account; the tools inherit
  that reach
- `MCP_AUTH_TOKEN` is a static shared secret with no rotation, no per-client
  identity and no rate limiting; running the endpoint without a network boundary
  in front of it is out of scope
- an endpoint served without any bearer check under `MCP_ALLOW_UNAUTHENTICATED=1`
  — that variable is the documented opt-out, so it is the deployment's decision
  and not a bypass. A way to reach the *same* state without setting it — HTTP
  mode coming up unauthenticated although the variable is unset — is a finding
- titles, locations and descriptions reach the model as text it reads, so an
  event — an invitation from someone else, say — can carry instructions aimed
  at the model; the mitigation is the MCP client's tool-approval prompts

If you are unsure which side of that line a finding falls on, report it.
