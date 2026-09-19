# The verifier

Reconstructs a finished round from its **transaction history** and re-runs the
published algorithm against it. Nothing here reads live account state, so a
round stays verifiable after its account has been closed: closing reclaims the
rent, but the events the program emitted stay in the ledger.

Standard library only. No pip install, no network calls beyond the RPC you
point it at.

## What it trusts

Nothing in this repository, nothing in the registry, and not the RPC either —
provided you do three things:

1. Verify the GPG signature on the release tag. The fingerprint is in the
   top-level [`README.md`](../README.md), and confirming it out of band is the
   whole point.
2. Build the image yourself, or pull it and check its digest against the one in
   the signed release notes.
3. Point it at an RPC we do not operate.

Skip any of those and it degrades to taking our word for it.

## Running it

```bash
docker pull ghcr.io/pumplingxyz/vrf-verifier:vrf-vYYYY.MM.DD

docker run --rm \
  -e SOLANA_RPC=https://your-archival-rpc.example/path \
  ghcr.io/pumplingxyz/vrf-verifier:vrf-vYYYY.MM.DD \
  <ROUND_PUBKEY>
```

`SOLANA_RPC` must be **archival**. The default public endpoint prunes old
transactions; the scan then misses events and the run is worthless. The
verifier counts what it could not fetch and refuses to be quiet about it.

`--json` gives machine-readable output on stdout, with the human log on stderr.

Without Docker, from the repository root:

```bash
python3 vrf/verify.py <ROUND_PUBKEY> --vrf-script vrf.py
```

Convenient while developing, but it drops the reproducibility the pinned base
image buys you.

Building the image locally — note the context is the repository root, not this
directory, because the canonical `vrf.py` lives one level up:

```bash
docker build -f vrf/Dockerfile -t vrf-verifier:local .
docker run --rm vrf-verifier:local <ROUND_PUBKEY>
```

## What it checks

1. `sha256(vrf.py)` against the `vrf_algorithm_hash` the round committed in its
   `LotteryInitialized` event. Both values are printed either way.
2. Every transaction touching the round account, decoding `Deposit` events into
   per-coin stakes.
3. The seed, from `VrfFulfilled` — or from `EmergencySeedUsed` if the fallback
   path overrode it, in which case the last seed written wins and a warning is
   printed. That warning matters: see the top-level README on why a fallback
   seed is a weaker guarantee.
4. An integrity gate: the stakes it summed must equal `fee_amount + meme_amount`
   from `PurchasesPhaseStarted`, which is also the budget the targets are
   computed from. A mismatch means the RPC dropped transactions.
5. Re-runs the algorithm and prints wins, lamport targets, and a `VERDICT` line.

Exit codes: `0` verified, `1` hash mismatch, `2`–`5` the run could not be
completed — no transactions, no stakes, no seed, or the integrity gate failed.

**What it does not do.** It does not compare against the purchases the keeper
actually made — DEX state, slippage and routing are a separate problem and not
this image's job. It also aggregates `Deposit` events itself rather than using
the committed `weights_payload`; checking that commitment is step 1 of the
manual flow in the top-level README.

## Files

- `verify.py` — the verifier.
- `verify.sh` — entrypoint: prints `sha256(vrf.py)`, then runs it.
- `Dockerfile` — base image pinned by digest, copy layout, entrypoint.
- `.dockerignore` — keeps the build context small.

`vrf.py` sits at the repository root rather than in here, because it is the
file we want read first. The build context picks it up from there.

## Event names are protocol, not prose

Anchor derives an event's 8-byte discriminator from its name:
`sha256("event:<Name>")[:8]`. The names in `verify.py` therefore have to match
the `#[event]` struct names in the deployed program character for character,
and the byte offsets have to match those structs' field order.

Get either wrong and nothing fails loudly — the discriminator simply never
matches, every event is skipped, and the verifier reports an empty
reconstruction. So the names are not editable for style, and neither are they
a naming decision: they are part of the deployed protocol. The prose in these
docs uses its own vocabulary on purpose; the identifiers stay as deployed.

Re-check names and offsets against the IDL before every release, and in the
same commit as any program change.

## Keeping `vrf.py` in sync

The published `vrf.py` must stay byte-for-byte identical to the implementation
that runs in production — the VRF engine module in the main repository.
Production is the ground truth: it is what decides rounds. Any divergence breaks verifiability, and it breaks it silently, because
the hash comparison will simply start failing for every round.

The two files are deliberately structurally identical, so syncing is one `cp`.
No build step, no codegen.

```bash
ENGINE=/path/to/vrf_engine.py

cp "$ENGINE" vrf.py
diff -q "$ENGINE" vrf.py
sha256sum vrf.py
python3 vrf/verify.py --help
```

That `sha256sum` goes into the release notes and into the next
`vrf_algorithm_hash` used at `initialize`. In the main repository,
`scripts/vrf_algorithm_hash.py --check` exits non-zero when the committed value
has drifted from the source — run it there before tagging here.

## Before publishing a tag

- [ ] `vrf.py` synced; `diff` reports nothing.
- [ ] `scripts/vrf_algorithm_hash.py --check` passes in the main repository.
- [ ] Event names and offsets re-checked against the current IDL.
- [ ] `sha256(vrf.py)` recorded in the release notes.
- [ ] `python3 vrf/verify.py --help` runs.
- [ ] Base image digest in `Dockerfile` still pinned.
- [ ] Tag created with `git tag -s vrf-vYYYY.MM.DD` from the release identity.
- [ ] Image pushed, digest recorded in the release notes.
- [ ] Admin runbook updated with the new sha256 for `initialize`.
