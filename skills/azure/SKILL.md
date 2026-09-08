---
name: azure
description: Work with Microsoft Azure and Azure DevOps through the Azure CLI. Use for any Azure task, including subscriptions, resources, logs, App Service, Azure SQL, pipelines, repositories, pull requests, or organization and project discovery. This skill provides platform access and scoping, not a prescribed testing, review, merge, deployment, or release workflow.
---

# Azure

Use the installed `az` CLI as the general Azure interface. It covers Azure
Resource Manager and, through the installed `azure-devops` extension, Azure
DevOps. Do not add a narrower connector or separate Azure DevOps plugin when
the CLI already exposes the requested operation.

## Establish Live Context

Start with the smallest read-only checks that identify the live surface:

```bash
az version --output json
az account show --output json
az account list --output table
az extension list --output table
```

For Azure DevOps, derive the organization, project, repository, and default
branch from the repository remote, repository instructions, or live Azure
queries. Do not carry those facts from another project.

```bash
git remote get-url origin
az devops configure --list
az repos show --organization ORG_URL --project PROJECT --repository REPOSITORY
```

Treat ambient Azure defaults as discovery hints, not mutation authority. Name
the subscription on Azure resource commands, and name the organization and
project on Azure DevOps commands. Add the repository argument whenever the
operation could otherwise select the wrong repository.

## Authentication

Inspect current authentication before acting. Use the user's existing Azure
CLI session when it has the required access. If authentication is absent or
expired, stop at that boundary unless the user asked to authenticate in the
current task.

When Azure resources and Azure DevOps use different Microsoft identities, use
the installed profile front doors instead of overwriting the ambient `az`
session:

```bash
az-work account show --output json
az-devops status
az-devops repos show --organization ORG_URL --project PROJECT --repository REPOSITORY
```

`az-work` uses the normal Azure config directory. `az-devops` uses its own
config directory and a non-secret local account selector, silently refreshes
the selected cached session, and passes the resulting Azure DevOps token only
to the child process. An empty `azureProfile.json` does not by itself prove
that the selected DevOps account is logged out; check `az-devops status`.

If `az-devops status` reports that renewal is required and the user authorized
authentication in the current task, run `az-devops login`. It uses Microsoft
device login and can be completed from a phone. Do not start a second device
flow while a cached session still validates. Use `az-devops login --force`
only when the user explicitly wants to replace that session.

Never print access tokens, refresh tokens, client secrets, connection strings,
PATs, service-principal credentials, or full credential files. Do not replace
an unavailable user session with a service principal or PAT unless the user
explicitly chooses that credential path.

### Git over HTTPS

Use the packaged `git-credential-az-devops` helper when Git must reuse the
configured DevOps identity. Keep the exact allowed repository URLs in the
private profile's `devops.git_urls` array; `az-devops configure --git-url URL`
sets that allowlist, with repeated options for multiple repositories. Existing
allowlisted URLs are preserved when `--git-url` is omitted. Do not put account,
organization, or private repository selectors in the public plugin source.

With the plugin's `bin` directory on `PATH`, set the destination repository's
`credential.https://dev.azure.com.useHttpPath` to `true` and its scoped helper
to `az-devops`. Clear other helper values for that scope before adding this
helper. See the README for the complete local configuration commands.

Read `git credential capability` first and require `capability authtype`.
The helper uses Git's negotiated ephemeral Bearer protocol, with exact HTTPS
host and repository-path checks before requesting a silent token. It never
falls back to a PAT/password guess or starts interactive authentication.
Do not invoke a live helper `get` or `git credential fill` in agent output;
their stdout contains credentials. Keep Git HTTP tracing disabled. Verify
access with a bounded ordinary Git operation such as `git ls-remote origin HEAD`.
If the silent session cannot be reused, follow the authentication boundary
above. Git `store` and `erase` do not persist or revoke credentials; the
existing MSAL cache remains provider-owned.

## Query Precisely

Bound broad reads with Azure's own query and output controls:

```bash
az resource list --subscription SUBSCRIPTION --query '[].{name:name,type:type,group:resourceGroup}' --output table
az group list --subscription SUBSCRIPTION --query '[].{name:name,location:location}' --output table
az repos pr list --organization ORG_URL --project PROJECT --repository REPOSITORY --status active --output table
```

Use `--query`, `--output`, service-specific limits, exact resource groups, and
exact names instead of dumping an entire tenant or log history into context.
Read official Azure CLI help or Microsoft Learn for a service-specific command
whose flags or behavior are uncertain.

## Make The Requested Change

Follow the user's exact request and the destination repository's `AGENTS.md`.
Azure ownership does not add a lifecycle of its own:

- do not prescribe tests, linting, coverage, review, snapshots, or UI audits;
- do not require a pull request, merge strategy, tracker transition, rollout
  watcher, or release proof unless the user or the repository requires it; and
- do not split ordinary implementation into a separate task merely because
  Azure hosts the repository or runtime.

Use the relevant `az` command directly, inspect the result, and verify the
requested Azure state in proportion to the operation. If the repository owns
a more specific Azure command or script, use it for its project facts.

## Safety Boundary

Read-only inspection is allowed when relevant. Before a destructive,
irreversible, access-widening, production-impacting, or billable operation,
resolve the exact subscription, resource group, resource, and effect. Ask for
the user's approval when that effect was not already explicit in the current
request.

This includes deletion, resource moves, broad IAM or role grants, firewall
changes, production restarts, billing or quota changes, secret rotation, and
commands that can replace or purge stored data. Prefer service-supported
preview, validation, `what-if`, or dry-run modes when available.

Do not open Azure Portal or another interactive UI unless the user asks for that
surface or the CLI cannot express the requested operation and the UI boundary
is explained first.
