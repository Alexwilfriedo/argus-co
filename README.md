# Argus for Codex

Argus is a Codex plugin for QA/QE work on web applications. It can help Codex run
or prepare live audits, demo captures, and a deterministic `@playwright/test`
non-regression harness with visual regression, accessibility, Core Web Vitals,
links, console/network health, security checks, JSON/JUnit/HTML reports, and CI
gating.

This repository is structured as a Codex marketplace:

```text
.agents/plugins/marketplace.json
plugins/argus/
  .codex-plugin/plugin.json
  skills/argus/
    SKILL.md
    references/
    scripts/install.sh
    assets/scaffold/
```

## Install Locally

Clone this repository, then add the repository root as a local Codex marketplace:

```bash
codex plugin marketplace add /path/to/argus-co
codex plugin add argus@personal
```

The marketplace display name is `Argus Codex`; the marketplace identifier remains
`personal` because the Codex scaffold uses that default for local marketplaces.

## Use

Start a new Codex thread after installation, then ask Codex to use Argus, for
example:

```text
Installe Argus dans ce projet et prepare la config.
Audite cette application web avec Argus.
Transforme ces findings QA en specs Playwright.
```

Argus will first inspect the target repo and infer the stack. It asks only for
blocking run configuration such as `BASE_URL`, `ENV`, and authentication details
when they cannot be inferred safely.

## Safety

Argus is read-only by default in production. Destructive actions, persistent
form submissions, checkout/payment/transfer flows, active scans, and external
network scans require explicit authorization and a safe staging/local target.
Secrets must come from environment variables such as `QA_USER`, `QA_PASS`, or CI
secrets.

## License

MIT - see [LICENSE](LICENSE).
