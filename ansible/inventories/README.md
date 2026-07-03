# Dynamic inventory — two-account discovery

Hosts have no public IPs and are managed via **SSM only** (PLAN §4.2). We do not
keep a static host list; instead the `amazon.aws.aws_ec2` plugin discovers
instances by the tags Terraform applies (PLAN §7):

```
Project=llt, Role=<generator|agg-t1|agg-t2|leader>,
Stack=<vector|cribl>, Os=<linux|windows>
```

and derives Ansible groups from them: `role_generator`, `role_agg_t1`,
`role_agg_t2`, `role_leader`, `stack_vector`, `stack_cribl`, `os_linux`,
`os_windows`.

## Sender vs logging account — chosen approach: TWO inventory files, one per profile

The experiment spans two AWS accounts (PLAN §4.1): a **sender** account
(generator hosts + agents) and a **logging** account (aggregators, Cribl leader,
buckets). The `aws_ec2` plugin's `aws_profile` option is **`type: str`** — it
queries exactly **one** account per inventory source. We therefore use **two
inventory config files** in this directory, one per profile:

| File | Account | Populates groups |
|------|---------|------------------|
| `sender.aws_ec2.yml`  | sender  | `role_generator`, agent stacks, both OS |
| `logging.aws_ec2.yml` | logging | `role_agg_t1/t2`, `role_leader`, Cribl stack |

Because `ansible.cfg` sets `inventory = inventories/` (this **directory**),
Ansible loads **both** files in one pass and unions the hosts. A single
`ansible-playbook` run therefore sees both accounts, and group membership is
tag-driven and identical across accounts, so `site.yml` configures sender +
logging hosts together.

### Why two files instead of one with a profile list
`aws_profile` is a scalar string, not a list; passing a list makes the plugin
fail with `unhashable type: 'list'`. Two single-profile files is the clean,
schema-valid way to cover two accounts and is one of the approaches PLAN §4.2
anticipates.

## Environment variables (exported by `scripts/setup.sh`; no secrets in repo)

```
LLT_AWS_PROFILE_SENDER    e.g. llt-sender
LLT_AWS_PROFILE_LOGGING   e.g. llt-logging
LLT_AWS_REGION            e.g. us-east-2   (single region, PLAN §4.1)
```

Each profile authenticates to its account via `~/.aws` (SSO/role/creds) — never
committed (PLAN §7).
