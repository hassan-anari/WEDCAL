# Controlled economic policy registration

The supported operator entry point is `register_economic_policy`, backed by permissioned services
in `apps/arena/policy_registration.py`. EconomicPolicyVersion and the new proposal/approval Admin
inspectors remain read-only. No SQL, Django shell edits, editable policy form or migration seed is
part of this workflow. Registration does not publish GameConfig, bind a Season or authorize workers.

Two different active staff accounts are required. The submitting account needs
`arena.propose_economic_policy`; the independent approving account needs
`arena.approve_economic_policy`. Even a superuser cannot approve its own proposal. Management-shell
access is a trusted operator boundary: restrict it through deployment IAM, identify the actual
operator using `--actor`, and retain the independent review ticket. The application enforces different
account IDs; organizational controls must ensure those accounts represent independent reviewers.

Prepare the complete policy JSON using the approved example and
[`economic-policy.schema.json`](../../config/schemas/economic-policy.schema.json). The service
rejects missing/unknown fields, booleans masquerading as integers, invalid participation maps,
unsupported recognition/Growth rules, unsupported Mission identities and invalid weight/cap sums.
It calls the existing economic validator; no weaker parallel validator awards money. The canonical
SHA-256 is `digest(payload)`, with sorted JSON keys and compact separators. File whitespace/order
does not change that checksum. The command rejects duplicate JSON keys and input over 1 MiB.

These are procedure examples, **not permission to run them on Production**:

```sh
# Author previews validation and checksum. No rows are created.
python manage.py register_economic_policy propose --actor AUTHOR --reference SOURCE_REVIEW \
  --key POLICY_KEY --policy-version 1 --file APPROVED_POLICY.json

# After reviewing the exact output, explicitly submit that checksum.
python manage.py register_economic_policy propose --actor AUTHOR --reference SOURCE_REVIEW \
  --key POLICY_KEY --policy-version 1 --file APPROVED_POLICY.json --checksum SHA256 --apply

# A different authorized reviewer independently inspects the immutable proposed payload.
# Dry-run returns validated_payload and submitted_by_id as well as the checksum.
python manage.py register_economic_policy approve --proposal PROPOSAL_ID --actor REVIEWER \
  --reference OWNER_APPROVAL --checksum SHA256

# Explicit approval and registration commit atomically; they do not activate economics.
python manage.py register_economic_policy approve --proposal PROPOSAL_ID --actor REVIEWER \
  --reference OWNER_APPROVAL --checksum SHA256 --apply
```

`--apply` is the only mutation switch. Submission requires the confirmed checksum. Approval always
requires it, revalidates the immutable proposal, locks that proposal, and registers the exact document
plus an immutable approval record in one transaction. Concurrent/repeated matching approvals return
one policy ID. Different actor/reference replays or conflicting key/version payloads are rejected.
The PostgreSQL approval guard also checks actor separation, key/version/payload/checksum equality and
matching approval reference. Proposal, approval and policy history cannot be edited or deleted.

For a new Season, explicitly select the resulting key/version/checksum in schema-4 GameConfig,
validate/publish that configuration through the existing workflow, then separately approve Season
activation/binding and automation control. Legacy registered policies retain their original audit
semantics; this migration does not fabricate missing historical proposal/approval events.

PostgreSQL evidence: `test_policy_registration.py` covers command dry-runs, strict schema/checksum,
permissions/inactive accounts, self-approval rejection, conflict/replay, concurrent approval, immutable
history and refusal of destructive downgrade. The registered-policy 15-day and 45-day simulations
exercise publication, activation/binding, weekly economics and Close. Synthetic registration evidence
is published with the CI report artifact and under `inspection/policy/`.
