# Microsoft Azure

Microsoft Azure and Azure DevOps through the installed `az` CLI.

## Boundary

This plugin owns the Azure platform surface: authentication discovery,
subscription and tenant scoping, resource operations, Azure DevOps
organizations and repositories, and safe CLI use.

It does not define an engineering lifecycle. Testing, linting, branches,
reviews, pull requests, merges, deployment, and release verification follow
the user's exact request and the destination repository's own instructions. The
plugin never invents an extra proof or tracker process merely because the
remote or runtime is hosted by Azure.

## Skill

| Skill | Use it when |
| --- | --- |
| `azure` | Any Microsoft Azure or Azure DevOps task that benefits from the Azure CLI. |

## Interface

Install the Azure CLI and its Azure DevOps extension before using these
commands. Prefer explicit subscription, organization, project, and repository
arguments over ambient defaults.

### Separate Azure identities

The plugin ships two stable front doors for machines where Azure resources and
Azure DevOps use different Microsoft identities:

```bash
az-work account show
az-devops status
az-devops pipelines list --organization ORG_URL --project PROJECT
```

- `az-work` isolates the normal Azure CLI profile. Its default config directory
  is `~/.azure`; override it with `AZURE_WORK_CONFIG_DIR` when needed.
- `az-devops` selects one cached Microsoft identity from a separate Azure CLI
  config directory, silently refreshes an Azure DevOps access token, and passes
  that token only to the child CLI process. It never prints or stores the token
  as a plugin setting.

Configure the non-secret account selector once:

```bash
az-devops configure \
  --username you@example.com \
  --config-dir ~/.azure-devops-personal
az-devops login
az-devops devops configure --defaults organization=ORG_URL project=PROJECT
```

`az-devops login` is normally a no-op while the cached session is renewable.
If renewal is genuinely required, it displays Microsoft's device-login URL and
short-lived code so approval can happen from another device. Use
`az-devops login --force` only to deliberately replace the cached session.

The local selector is stored at
`~/.config/azure-plugin/profiles.json` with mode `0600`. It contains profile
paths and an account name, not a password, refresh token, access token, PAT, or
client secret. See `examples/profiles.json` for its shape.

### Git access through the same identity

`git-credential-az-devops` reuses the selected Azure DevOps identity for Git
HTTPS operations. Add each exact repository URL to the private profile:

```bash
az-devops configure \
  --username you@example.com \
  --config-dir ~/.azure-devops-personal \
  --git-url 'https://dev.azure.com/ORG/PROJECT/_git/REPOSITORY'
```

Repeat `--git-url` for every allowed repository. Providing this option replaces
the allowlist; omitting it preserves the existing allowlist. Use URLs without
an embedded username, password, query string, or fragment. The `devops.git_urls`
array stays in the owner-only profile file alongside the account selector.

Make the plugin's `bin` directory available on `PATH`, then configure the
destination repository to use the helper:

```bash
git credential capability
git config --local credential.https://dev.azure.com.useHttpPath true
git config --local credential.https://dev.azure.com.helper ''
git config --local --add credential.https://dev.azure.com.helper az-devops
git ls-remote origin HEAD
```

Git must advertise `capability authtype`. The helper negotiates an ephemeral
Bearer credential with Git; it does not treat an OAuth token as a PAT or
password. Only HTTPS requests to `dev.azure.com` with an exact allowlisted
repository path receive a token. A renewable cached session is required;
the helper never starts sign-in. Git's `store` and `erase` operations are
no-ops. Normal MSAL refreshes may update its existing owner-only provider cache.

Invoke the helper only through Git. Do not run its `get` operation or
`git credential fill` directly with a live profile, because those commands
print credentials. Do not enable Git HTTP tracing or capture credential
protocol output in agent context, logs, files, or shell variables.

## Install

```bash
claude plugin install azure@package-manager
```

```bash
codex plugin add azure@package-manager
```
