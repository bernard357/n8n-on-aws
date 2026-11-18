# 6. n8n editions and licence enforcement

Sources, read 2026-11-18: [LICENSE.md](https://github.com/n8n-io/n8n/blob/master/LICENSE.md) (Sustainable
Use License v1.0), [pricing](https://n8n.io/pricing/),
[community edition features](https://docs.n8n.io/hosting/community-edition-features/),
[manage your license](https://docs.n8n.io/deploy/host-n8n/configure-n8n/manage-your-license.md),
[OEM integration](https://docs.n8n.io/deploy/host-n8n/deploy-as-an-oem-integration.md), and the feature
pages listed in [03-deployment-options.md](03-deployment-options.md).

This is not legal advice. The tool encodes n8n's published terms as validation rules so that users see
them before deploying. The terms themselves stay between the user and n8n.

## 6.1 Self-hosted editions

| Edition | How you get it | Licence key | Notes |
|---|---|---|---|
| `community` | Free, from GitHub or Docker | none | Sustainable Use License. "Almost the complete feature set." |
| `registered-community` | Free, register an email address in the UI | free activation key | Adds folders, debug in editor, custom execution data |
| `business` | Paid, self-hosted only, priced on production executions (40K/month tier) | activation key | Pricing page: "For companies with < 100 employees" |
| `enterprise` | Paid, contact sales | activation key or offline certificate | All features, support with SLA |

The Starter and Pro plans are n8n Cloud only. The tool rejects them with a hint.

Paid keys ping n8n's licence server, which runs on Cloudflare, every day. The deployment therefore needs
internet egress, or an offline certificate (`N8N_LICENSE_CERT`) for air-gapped installs. Set
`N8N_LICENSE_ACTIVATION_KEY` **or** `N8N_LICENSE_CERT`, never both. One key can serve any number of
instances; usage is pooled.

## 6.2 Usage boundaries of the community editions (Sustainable Use License)

Quoted from LICENSE.md, which is short and explicit:

- Use is permitted "only for your own internal business purposes or for non-commercial or personal use".
- Distribution is permitted "only if you do so free of charge for non-commercial purposes".
- `.ee.` source files (enterprise features) require an n8n Enterprise License.

From the OEM page:

- Showing the n8n **editor** to your own customers inside your product needs a separate **OEM
  agreement** with n8n.
- Using n8n as a hidden back end (webhooks or API, users never see n8n) is allowed on all paid plans
  under the standard licence.

## 6.3 How the tool enforces it

The YAML records the edition and, for community editions, a usage declaration:

```yaml
n8n:
  license:
    edition: community            # community | registered-community | business | enterprise
    usage:
      purpose: internal-business  # internal-business | personal | non-commercial
                                  # | service-for-third-parties | embedded-editor
      accept_sustainable_use_license: true
```

Rules applied by `validate`, `plan` and `deploy`:

| # | Condition | Result |
|---|---|---|
| L1 | `community*` without `usage` or without `accept_sustainable_use_license: true` | **error**: the user must declare usage |
| L2 | `community*` with `purpose ∈ {service-for-third-parties, embedded-editor}` | **error**: outside the Sustainable Use License; use a paid edition and contact n8n |
| L3 | paid edition with `purpose: embedded-editor` | **warning**: needs an OEM agreement with n8n (license@n8n.io) |
| L4 | paid edition with `purpose: service-for-third-parties` | **warning**: check the terms with n8n |
| L5 | `business` with `usage.employees ≥ 100` | **warning**: the Business plan is aimed at companies with fewer than 100 employees |
| L6 | edition ≠ `community` without `activation_key` or `certificate`; or with both | **error** |
| L7 | feature gate (§6.4) not met by the edition | **error**, with the feature and the minimum edition |
| L8 | raw `n8n.env` sets a gated variable (`N8N_MULTI_MAIN_SETUP_ENABLED`, `N8N_SSO_*`, `N8N_EXTERNAL_STORAGE_*`, binary or execution data mode `s3`, `N8N_LOG_STREAMING*`, `N8N_EXTERNAL_SECRETS*`) | same as L7, so the escape hatch cannot bypass the gate |
| L9 | paid edition with `activation_key` and no internet egress (`nat: none` and private tasks) | **error**: the licence server is unreachable; use `certificate` or enable egress |
| L10 | `version` predates a feature's minimum version (for example env-managed SSO before 2.18.0) | **error** |

The generated stacks carry a `n8n-on-aws:edition` tag. `plan` prints the edition, the declared purpose and
the gated features in use, so reviewers see them.

## 6.4 Feature gates (self-hosted)

| Feature | community | registered | business | enterprise |
|---|---|---|---|---|
| Queue mode, workers, webhook processors | ✓ | ✓ | ✓ | ✓ |
| Folders, debug in editor, custom execution data | — | ✓ | ✓ | ✓ |
| SSO (OIDC, SAML, LDAP) | — | — | ✓ | ✓ |
| External storage (S3) for binary and execution data | — | — | ✓ | ✓ |
| Environments, Git source control, projects | — | — | ✓ (6 projects) | ✓ |
| Multi-main | — | — | — | ✓ |
| External secrets store, log streaming, workers view | — | — | — | ✓ |

The sources disagree in places. The pricing table lists multi-main as "coming soon" while the docs
describe it as available on Enterprise, and it lists "Queue mode for scaling" on the Business card while
the edition page says queue mode is included in Community. We follow the **docs**. The gate table lives in
one data file (`licensing.py`) so it can be corrected in one place.

## 6.5 Assistant questions (community path)

1. "Which n8n edition will you run?" Choices show the price basis and what each edition adds.
2. For community editions: "Who will use this n8n instance?"
   - *My organisation's staff, for our own processes* → `internal-business`
   - *Just me, or a non-profit/hobby project* → `personal` / `non-commercial`
   - *Our customers or other third parties, or we charge for access* → `service-for-third-parties` → explains L2 and offers to switch to a paid edition
   - *n8n's editor shown inside our product* → `embedded-editor` → explains L2 and the OEM agreement
3. "Do you accept the Sustainable Use License (link)?" → `accept_sustainable_use_license`
4. For business: "How many employees does your company have?" → `usage.employees`, then L5.
5. Later questions hide or annotate options the edition cannot use, for example "S3 binary data
   (Business+)".
