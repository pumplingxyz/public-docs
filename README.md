# Pumpling — Public Docs

Pumpling is a memecoin promotion pool on Solana. Anyone names a coin and puts
SOL behind it; when the pool closes, a verifiable random draw sets each coin's
share of the buying, and the SOL goes out in on-chain purchases.

This repository holds the parts you need to check that a finished round was
decided honestly, without asking us for anything and without trusting us for
anything.

## What is here

| File | What it is |
|---|---|
| `vrf.py` | The algorithm that turns the random seed into each coin's share. Its sha256 is committed on-chain when a round starts. |
| `weights_commitment.py` | The rule for building the stake list whose hash is pinned on-chain before the draw. |
| `vrf/` | A stdlib-only verifier, and a Docker image that runs it. |
| `idl/idl.json` | The on-chain program's Anchor interface: instructions, accounts, events. |

---

## How a round is decided

Three things are fixed on-chain before anyone knows the outcome, and the draw
is a pure function of them.

**The stakes.** When the pool closes, the program receives `weights_hash` — a
sha256 over the list of who put how much behind which coin. Pinning it before
the draw means the numbers cannot be adjusted afterwards. The exact rule for
building the string that gets hashed is `weights_commitment.py`: sum per coin
in whole lamports, drop anything that sums to zero, sort by descending amount
and by coin address when equal, write it as `[["<mint>",<lamports>],…]`, hash
the UTF-8 bytes.

Lamports, because that is what the chain counts and what the draw weighs by. An
integer also has exactly one spelling, so there is no number formatting for a
third-party verifier to get subtly wrong.

Rounds opened before 2026-09-24 used an earlier rule: the same list, but
amounts in SOL rounded to eight decimals. Eight decimals of SOL is ten
lamports, so the commitment pinned a slightly different figure from the one the
draw used. It never moved a share by anything a person could see, but it meant
a round could not be reproduced from its commitment alone, which is the whole
point of publishing one. Both rules live in `weights_commitment.py` so those
rounds still verify.

**The seed.** A 32-byte value derived from ORAO VRF
(`VRFzZoJdhFWL8rkvu87LpKM3RbcVezpMEc6X5GVDr7y`, the same program on devnet and
mainnet), committed on-chain after the pool closes.

What makes it checkable is that the request seed is **derived, never supplied**:

```
force = sha256("pumpling-vrf-force-v1" || round || weights_hash || slot_hash)
```

The ORAO randomness account is a PDA of that `force`, so anyone can recompute
it and confirm the round asked the account it was supposed to ask — nobody
shopped for a friendlier one. The `slot_hash` is the hash of a recent slot,
named by the caller and at most 128 slots old. None of those hashes exist
before the pool closes, and no client can predict which slot its transaction
will land in, so the choice is between values nobody can steer.

ORAO answers with 64 bytes, and the round stores 32:

```
seed = sha256("pumpling-vrf-seed-v1" || randomness)
```

Hashed rather than truncated, so every byte the oracle produced has a say.

`Phase2Started` publishes `weights_hash`, `force`, the randomness account and
the slot, which is everything needed to recheck the derivation without reading
the round's account at all.

**The algorithm.** At `initialize` the round stores `vrf_algorithm_hash`, the
sha256 of `vrf.py` as a whole file. Comments included: editing a comment moves
the commitment exactly as much as editing the formula does. Rounds started
after a change carry the new value, and older rounds keep the old one.

### The draw

`vrf.py` runs `k` independent draws over the stakes:

```
k = min(70, max(number of coins, floor(6.7 * sqrt(pool in SOL))))
```

The constants are set for the 111 SOL pool cap — `k = 70` is reached at
109.2 SOL, just under a full pool. Each draw derives a 64-bit number from the
seed with keccak256 and picks a coin proportionally to its share of the pool.
Rejection sampling removes modulo bias, and each draw chains into the next
through `sha3_256(seed + b"|next")`, so the whole sequence follows from the
one committed seed.

Wins are then multiplied by `budget // k` to give each coin its lamport target.
The budget is the pool minus the 3% fee.

`k` does not set how wide the spread is — it sets how random it is. On average
a coin gets the share of the buying that matches the share of the SOL behind
it, and `k` is how far a single round strays from that average. With 15 coins
and `k = 70` the deviation is about 16.4%. **A coin can come out with nothing**,
and small stakes swing the hardest. This is a property of the design, not an
edge case.

### The fallback seed — read this before trusting a round

If ORAO stays silent for 120 seconds after the request, an admin may supply
the 32 bytes directly. The program refuses the moment the oracle's randomness
is actually there, so the wait cannot be used to shop for a better result at
any length — it only gives the oracle its fair chance. That path emits `EmergencySeedUsed`
instead of `VrfFulfilled`, and the program cannot check that such a seed is
random.

So verification proves the algorithm was applied honestly to whatever seed is
on-chain. For a `VrfFulfilled` round that is end-to-end fairness. For an
`EmergencySeedUsed` round it is not: the fairness of that draw reduces to
trusting us. The verifier prints a loud warning and flags `seed_source` in its
output whenever this path was used, and the site says so too.

We would rather publish this than have someone find it. A future change can
make the fallback seed unbiased as well — derived from a future blockhash the
admin cannot pre-select. Until then, treat those rounds as trust-in-operator.

---

## Reading the chain directly

`idl/idl.json` is the program's Anchor interface. Indexers and clients read a
round straight from the chain with it, with no API of ours in the path.

**Program address:** `4mk8SH9un549ETZatKRkths44e2RBRkFGBmTvFie2oeH`

The events verification is built on:

| Event | What it carries |
|---|---|
| `Deposit` | one stake: the coin, and the amount in lamports |
| `Phase2Started` | `weights_hash`, the derived `force`, the randomness account and the slot its hash came from |
| `VrfFulfilled` | the seed, folded from the oracle's answer |
| `EmergencySeedUsed` | the seed, supplied by an admin on the fallback path |
| `PurchasesPhaseStarted` | the fee and the buying budget |
| `LotteryInitialized` | `vrf_algorithm_hash`, the algorithm fingerprint |

Identifiers here are fixed by the deployed program. Anchor derives each event's
discriminator from its name, so the names are part of the protocol: renaming
one does not rename a concept, it breaks every reader silently. This
documentation keeps its own vocabulary; the interface keeps the names it was
deployed with.

---

## Check a round yourself

Four steps. Each one is checkable on its own, and none of them requires us to
be honest.

1. **The stakes.** Take `weights_payload` from the round's verification data,
   compute its sha256, and compare with the on-chain `weights_hash`. That hash
   is readable from the round's account while it is open, and from the
   `Phase2Started` event after the account is closed.
2. **The seed.** Take `vrf_seed` from the same round — on-chain it arrives in
   the `VrfFulfilled` event, or `EmergencySeedUsed` on the fallback path.
3. **The algorithm.** Compute the sha256 of `vrf.py` at the release tag the
   round announces, and compare with the round's `vrf_algorithm_hash` from the
   `LotteryInitialized` event.
4. **The result.** Run the algorithm on that seed and those stakes, and compare
   the shares it produces against the purchases that were actually made.

Steps 1 and 3 are one line each:

```bash
python3 - <<'PY'
import weights_commitment as w
payload = open("weights_payload.txt", encoding="utf-8").read().strip()
print(w.weights_hash_hex(payload))
PY

sha256sum vrf.py
```

Step 4, with the seed and budget the round announced:

```bash
python3 - <<'PY'
import json
from vrf import VrfEngine

payload = open("weights_payload.txt", encoding="utf-8").read().strip()
weights = {mint: int(lamports) for mint, lamports in json.loads(payload)}

seed = bytes.fromhex("<vrf_seed>")
budget = <budget in lamports>

k = VrfEngine.compute_k(weights)
wins = VrfEngine.draw_mint_wins(seed, weights, k)
targets = VrfEngine.compute_targets(wins, budget, k)

print("k      :", k)
print("wins   :", wins)
print("targets:", targets)
PY
```

### Or let the verifier do it

`vrf/` holds a verifier that reconstructs a round from its **transaction
history** rather than from data we hand it. It reads the events the program
emitted, aggregates the stakes itself, and re-runs the published algorithm. It
works after the round's account has been closed, because closing the account
reclaims its rent but leaves every emitted event in the ledger.

```bash
docker run --rm \
  -e SOLANA_RPC=https://your-archival-rpc.example/path \
  ghcr.io/pumplingxyz/vrf-verifier:vrf-vYYYY.MM.DD \
  <ROUND_PUBKEY>
```

The RPC must be **archival**. Public endpoints prune old transactions, the scan
then misses events, and the verifier says so rather than pretending. Pick an
endpoint we do not run.

Output ends in `VERDICT: VERIFIED` or `VERDICT: HASH_MISMATCH`. Details, flags
and what the verifier deliberately does not check are in
[`vrf/README.md`](vrf/README.md).

---

## The full trust chain

The quick path above trusts that the image on GHCR is the one we published.
Removing that assumption takes five steps, done once and reused for every
round afterwards.

### 1. Pin the trust root

The release signing key has the fingerprint:

```
07E5 A590 BCF4 A467 A918  4E80 E256 C0B4 5EB1 2CCA
```

Everything else hangs off this one value, so confirm it out of band before
anything else. Use at least two independent sources — the pinned post on
[@pumplingxyz](https://x.com/pumplingxyz), an audit report, an archive.org
snapshot of [pumpling.xyz](https://pumpling.xyz). This README should match too,
but it sits in a repository we control, so on its own it proves nothing.

### 2. Verify the signed tag

```bash
git clone https://github.com/pumplingxyz/public-docs
cd public-docs
gpg --import release-key.asc
gpg --fingerprint 07E5A590BCF4A467A9184E80E256C0B45EB12CCA
git verify-tag vrf-vYYYY.MM.DD
```

`gpg: Good signature` with the matching fingerprint means the whole tree at
that tag is authentic — `vrf.py`, the commitment rule, the verifier source.
The key is also mirrored on a keyserver:

```bash
gpg --keyserver hkps://keyserver.ubuntu.com --recv-keys 07E5A590BCF4A467A9184E80E256C0B45EB12CCA
```

### 3. Hash the algorithm at that tag

```bash
git checkout vrf-vYYYY.MM.DD
sha256sum vrf.py
```

At the current release:

```
00a9da1268f2d909dbf6700a15ec3f902630c5194306bfffcf713150279d831b
```

This must equal the `vrf_algorithm_hash` the round committed.

### 4. Pin the image by digest

Every release publishes an image digest. Use it instead of the tag:

```bash
docker pull ghcr.io/pumplingxyz/vrf-verifier@sha256:<image-digest>
```

The image is built by a GitHub Actions workflow from the signed tag, and the
workflow refuses to publish if the signature does not verify. The base image is
pinned by digest in `vrf/Dockerfile`, so you can rebuild from the signed
checkout and get the same bytes.

### 5. Run it against your own RPC

```bash
docker run --rm \
  -e SOLANA_RPC=https://your-trusted-archival-rpc.example/path \
  ghcr.io/pumplingxyz/vrf-verifier@sha256:<image-digest> \
  <ROUND_PUBKEY>
```

What you are trusting at this point: the fingerprint you pinned in step 1,
ORAO for the seed, and the RPC you chose. Our website, this repository's
hosting and the container registry are all still in the path, but none of them
can change the answer without colluding with one of those three.

---

## What every round publishes

Announced per round, so both paths above are reproducible without contacting us:

```
Round #N
- Round account:      <PUBKEY>
- VRF release tag:    vrf-vYYYY.MM.DD
- sha256(vrf.py):     <hex>
- Image digest:       sha256:<hex>
- VRF fulfil tx:      <signature>
- Purchase txs:       [<signature>, ...]
```

## Contact

Integration questions, bug reports and independent audits:
`pumpling.xyz@gmail.com`.
