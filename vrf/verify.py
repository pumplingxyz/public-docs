"""
Independent verifier for a Pumpling round's VRF draw.

Works entirely from the **transaction history** of the round account, so it
verifies a round even after its on-chain account has been closed (the
program reclaims rent via `close_round`, which deallocates the account — its
data is gone, but the emitted events live forever in the ledger).

What it does:
  1. Scans every transaction that touched the round account and decodes the
     Anchor events it emitted:
       - `LotteryInitialized` -> vrf_algorithm_hash committed at initialize
       - `Deposit`            -> per-mint weights
       - `VrfFulfilled` / `EmergencySeedUsed` -> the VRF seed actually used
       - `PurchasesPhaseStarted` -> fee_amount + meme_amount (the buy budget)
  2. Computes sha256 of the bundled vrf.py and compares it with the
     `vrf_algorithm_hash` the admin attested at initialize time.
  3. Re-runs the canonical vrf.py against the seed and reconstructed weights
     and prints the resulting wins / targets.

What it deliberately does NOT do:
  - Compare against actual keeper purchases. Purchase verification is a
    separate concern (DEX state diffs, slippage etc.).

IMPORTANT — use an *archival* RPC. Public RPCs prune old transactions, so
`getTransaction` returns null for them and the scan misses events. Point
--rpc / $SOLANA_RPC at an archival endpoint you trust (and not one operated
by the Pumpling team).

The script depends only on the Python standard library so the Docker image
stays small and reproducible.
"""

import argparse
import base64
import hashlib
import json
import os
import struct
import sys
import urllib.request
import urllib.error
from typing import Dict, List, Optional, Tuple

# ----------------------------- constants ----------------------------------

DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
PAGE_LIMIT = 1000  # max for getSignaturesForAddress per Solana JSON-RPC docs

# ----------------------------- base58 -------------------------------------

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * pad + out


# ----------------------------- RPC ----------------------------------------


class RpcError(Exception):
    pass


def rpc(url: str, method: str, params: list) -> dict:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RpcError(f"HTTP {e.code}: {e.read().decode(errors='replace')}") from e
    except urllib.error.URLError as e:
        raise RpcError(f"network error talking to {url}: {e.reason}") from e
    if "error" in payload:
        raise RpcError(f"{method}: {payload['error']}")
    return payload["result"]


# ----------------------------- Anchor decode ------------------------------


def anchor_event_discriminator(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode()).digest()[:8]


# Event payload layouts (borsh, after the 8-byte event discriminator).
#
# These strings are protocol, not prose. Anchor derives an event's 8-byte
# discriminator from its name — sha256("event:<Name>")[:8] — so they have to
# match the `#[event]` struct names in the deployed program character for
# character. A rename on-chain stops every match here silently: no error, just
# an empty reconstruction. The offsets below assume those structs' field order.
# Verify both against the IDL before every release.
_EV = {name: anchor_event_discriminator(name) for name in (
    "LotteryInitialized",
    "Deposit",
    "Phase2Started",
    "VrfFulfilled",
    "EmergencySeedUsed",
    "PurchasesPhaseStarted",
)}


def _event_round(body: bytes) -> str:
    # Every event's first field is the round account pubkey.
    return b58encode(body[0:32])


# ----------------------------- transaction iteration ----------------------


def fetch_all_signatures(rpc_url: str, address: str) -> List[str]:
    """Newest-first list of successful signatures touching `address`."""
    sigs: List[str] = []
    before: Optional[str] = None
    while True:
        params: list = [address, {"limit": PAGE_LIMIT}]
        if before:
            params[1]["before"] = before
        page = rpc(rpc_url, "getSignaturesForAddress", params)
        if not page:
            break
        for entry in page:
            if entry.get("err") is None:
                sigs.append(entry["signature"])
        if len(page) < PAGE_LIMIT:
            break
        before = page[-1]["signature"]
    return sigs


def fetch_logs(rpc_url: str, signature: str) -> Optional[List[str]]:
    """Return log messages, or None if the RPC has no record of the tx
    (pruned history) — distinct from an empty log list."""
    tx = rpc(
        rpc_url,
        "getTransaction",
        [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
    )
    if tx is None:
        return None
    if not tx.get("meta"):
        return []
    return tx["meta"].get("logMessages") or []


class Reconstruction:
    def __init__(self) -> None:
        self.vrf_algorithm_hash: Optional[bytes] = None
        # from Phase2Started
        self.weights_hash: Optional[bytes] = None
        self.randomness_account: Optional[str] = None
        self.force: Optional[bytes] = None
        self.seed_slot: Optional[int] = None
        self.seed: Optional[bytes] = None          # last seed written wins
        self.seed_source: Optional[str] = None
        self.vrf_requested_ts: Optional[int] = None  # from Phase2Started
        self.emergency_ts: Optional[int] = None      # from EmergencySeedUsed
        self.weights: Dict[str, int] = {}
        self.n_deposits: int = 0
        self.fee_amount: Optional[int] = None
        self.meme_amount: Optional[int] = None
        self.n_tx: int = 0
        self.n_pruned: int = 0


def scan_history(rpc_url: str, round_account: str) -> Reconstruction:
    """Single chronological pass over the round's transactions, decoding
    every relevant Anchor event."""
    r = Reconstruction()
    # getSignaturesForAddress is newest-first; reverse for chronological order
    # so the *last* seed event (e.g. an emergency override) wins.
    sigs = list(reversed(fetch_all_signatures(rpc_url, round_account)))
    r.n_tx = len(sigs)
    print(f"  scanning {len(sigs)} transactions...", file=sys.stderr)
    for i, sig in enumerate(sigs):
        if i and i % 100 == 0:
            print(f"  ...{i}/{len(sigs)}", file=sys.stderr)
        logs = fetch_logs(rpc_url, sig)
        if logs is None:
            r.n_pruned += 1
            continue
        for line in logs:
            if not line.startswith("Program data: "):
                continue
            try:
                blob = base64.b64decode(line[len("Program data: ") :].strip())
            except Exception:
                continue
            if len(blob) < 8:
                continue
            disc, body = blob[:8], blob[8:]

            if disc == _EV["Deposit"] and len(body) == 112:
                if _event_round(body) != round_account:
                    continue
                mint = b58encode(body[64:96])
                (amount,) = struct.unpack("<Q", body[96:104])
                r.weights[mint] = r.weights.get(mint, 0) + amount
                r.n_deposits += 1

            elif disc == _EV["LotteryInitialized"] and len(body) >= 136:
                if _event_round(body) != round_account:
                    continue
                r.vrf_algorithm_hash = body[104:136]  # last field

            elif disc == _EV["Phase2Started"] and len(body) >= 152:
                if _event_round(body) != round_account:
                    continue
                r.weights_hash = body[32:64]
                r.randomness_account = b58encode(body[64:96])
                r.force = body[96:128]
                (r.seed_slot,) = struct.unpack("<Q", body[128:136])
                (r.vrf_requested_ts,) = struct.unpack("<q", body[136:144])

            elif disc == _EV["VrfFulfilled"] and len(body) >= 64:
                if _event_round(body) != round_account:
                    continue
                r.seed = body[32:64]
                r.seed_source = "VrfFulfilled"

            elif disc == _EV["EmergencySeedUsed"] and len(body) >= 80:
                if _event_round(body) != round_account:
                    continue
                r.seed = body[32:64]
                r.seed_source = "EmergencySeedUsed"
                (r.vrf_requested_ts,) = struct.unpack("<q", body[64:72])
                (r.emergency_ts,) = struct.unpack("<q", body[72:80])

            elif disc == _EV["PurchasesPhaseStarted"] and len(body) >= 48:
                if _event_round(body) != round_account:
                    continue
                r.fee_amount, r.meme_amount = struct.unpack("<QQ", body[32:48])
    return r


# ----------------------------- vrf.py loader ------------------------------


def load_vrf_module(vrf_path: str):
    """Import vrf.py by absolute path without polluting the import system."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("vrf", vrf_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load vrf.py from {vrf_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ----------------------------- main ---------------------------------------


def _default_vrf_script() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "vrf.py"),         # Docker layout
        os.path.join(here, "..", "vrf.py"),   # repo-root layout
    ]
    for path in candidates:
        if os.path.isfile(path):
            return os.path.abspath(path)
    return candidates[0]


def main() -> int:
    p = argparse.ArgumentParser(
        description="Verify a Pumpling round's VRF draw from its transaction history."
    )
    p.add_argument(
        "round_account",
        metavar="ROUND_PUBKEY",
        help="Round account pubkey (base58)",
    )
    p.add_argument(
        "--rpc",
        default=os.environ.get("SOLANA_RPC", DEFAULT_RPC),
        help=(
            f"Solana JSON-RPC endpoint (default: {DEFAULT_RPC} / $SOLANA_RPC). "
            "Must be ARCHIVAL (public RPCs prune old transactions). Use an "
            "independent endpoint — not one operated by the Pumpling team."
        ),
    )
    p.add_argument(
        "--vrf-script",
        default=_default_vrf_script(),
        help=(
            "Path to canonical vrf.py. By default looks next to verify.py "
            "(Docker layout: /app/vrf.py) and then one directory up "
            "(repo-root layout: <root>/vrf.py)."
        ),
    )
    p.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON to stdout."
    )
    args = p.parse_args()

    # 1. sha256(vrf.py)
    with open(args.vrf_script, "rb") as f:
        vrf_bytes = f.read()
    vrf_sha = hashlib.sha256(vrf_bytes).digest()

    print(f"[..] scanning round {args.round_account} via {args.rpc}", file=sys.stderr)

    # 2. reconstruct from transaction history
    r = scan_history(args.rpc, args.round_account)
    if r.n_tx == 0:
        print(f"[!!] no transactions found for {args.round_account}", file=sys.stderr)
        return 2
    if r.n_pruned:
        print(
            f"[!!] {r.n_pruned}/{r.n_tx} transactions are not available on this "
            f"RPC (pruned history). Re-run with an ARCHIVAL RPC, or the draw "
            f"reconstruction below is incomplete and untrustworthy.",
            file=sys.stderr,
        )

    if r.vrf_algorithm_hash is None:
        print("[!!] no LotteryInitialized event found — cannot read the "
              "attested vrf_algorithm_hash (wrong account or pruned RPC).",
              file=sys.stderr)
        return 2

    hash_match = r.vrf_algorithm_hash == vrf_sha
    print(f"[..] sha256(vrf.py) = {vrf_sha.hex()}", file=sys.stderr)
    print(f"[..] on-chain hash  = {r.vrf_algorithm_hash.hex()}", file=sys.stderr)
    if hash_match:
        print("[ok] vrf.py hash matches attested vrf_algorithm_hash", file=sys.stderr)
    else:
        print(
            "[!!] HASH MISMATCH — bundled vrf.py is not the algorithm the "
            "admin attested at initialize time.",
            file=sys.stderr,
        )

    if not r.weights:
        print("[!!] no Deposit events found for this round", file=sys.stderr)
        return 3
    print(
        f"[ok] aggregated {r.n_deposits} deposits across {len(r.weights)} mints",
        file=sys.stderr,
    )

    pool = sum(r.weights.values())

    # 2b. integrity: fee_amount + meme_amount (from PurchasesPhaseStarted) must
    # equal the summed deposits. A mismatch means we missed deposit events
    # (pruned/dropped txs) — the draw cannot be trusted.
    if r.meme_amount is None or r.fee_amount is None:
        print("[!!] no PurchasesPhaseStarted event — round never reached the "
              "purchase phase, or the RPC dropped it. Cannot derive budget.",
              file=sys.stderr)
        return 4
    if r.fee_amount + r.meme_amount != pool:
        print(
            f"[!!] integrity check failed: deposits sum to {pool} but "
            f"fee_amount+meme_amount={r.fee_amount + r.meme_amount}. "
            f"The RPC likely dropped transactions; re-run with an archival RPC.",
            file=sys.stderr,
        )
        return 5
    print(f"[ok] deposits sum ({pool}) == fee+budget from PurchasesPhaseStarted",
          file=sys.stderr)

    if r.seed is None:
        print("[!!] no VrfFulfilled/EmergencySeedUsed event — VRF has not "
              "been fulfilled for this round.", file=sys.stderr)
        return 4

    if r.seed_source == "EmergencySeedUsed":
        silence = ""
        if r.vrf_requested_ts and r.emergency_ts:
            silence = f" after {r.emergency_ts - r.vrf_requested_ts}s of oracle silence"
        print(
            "\n"
            "[WARNING] This round's seed comes from the EMERGENCY path "
            f"(EmergencySeedUsed{silence}), NOT from the VRF oracle.\n"
            "          On that path the admin supplies the 32 bytes directly. "
            "This verifier proves the algorithm was applied honestly to that "
            "seed, but it CANNOT prove the seed was random or unmanipulated. "
            "Treat the fairness of this draw as trust-in-operator, not "
            "trustless. A VERIFIED verdict below means only 'algorithm applied "
            "correctly to the on-chain seed'.\n",
            file=sys.stderr,
        )

    # 3. run vrf.py
    sys.path.insert(0, os.path.dirname(os.path.abspath(args.vrf_script)))
    engine = load_vrf_module(args.vrf_script).VrfEngine

    k = engine.compute_k(r.weights)
    wins = engine.draw_mint_wins(r.seed, r.weights, k)
    budget = r.meme_amount
    targets = engine.compute_targets(wins, budget, k)

    result = {
        "round": args.round_account,
        "rpc": args.rpc,
        "vrf_sha256": vrf_sha.hex(),
        "on_chain_vrf_algorithm_hash": r.vrf_algorithm_hash.hex(),
        "on_chain_weights_hash": r.weights_hash.hex() if r.weights_hash else None,
        "hash_match": hash_match,
        "vrf_seed": r.seed.hex(),
        "seed_source": r.seed_source,
        "randomness_account": r.randomness_account,
        "vrf_force": r.force.hex() if r.force else None,
        "seed_slot": r.seed_slot,
        "vrf_requested_ts": r.vrf_requested_ts,
        "emergency_ts": r.emergency_ts,
        "emergency_seed": r.seed_source == "EmergencySeedUsed",
        "deposits_count": r.n_deposits,
        "pool_lamports": pool,
        "fee_lamports": r.fee_amount,
        "budget_lamports": budget,
        "k": k,
        "wins": wins,
        "targets_lamports": targets,
        "verdict": "VERIFIED" if hash_match else "HASH_MISMATCH",
    }

    if args.json:
        json.dump(result, sys.stdout, indent=2, sort_keys=True, default=str)
        sys.stdout.write("\n")
    else:
        print()
        print(f"vrf.py sha256:           {vrf_sha.hex()}")
        print(f"on-chain hash:           {r.vrf_algorithm_hash.hex()}")
        wh = r.weights_hash.hex() if r.weights_hash else "not found"
        print(f"on-chain weights_hash:   {wh}")
        print(f"hash match:              {hash_match}")
        seed_label = r.seed_source
        if r.seed_source == "EmergencySeedUsed":
            seed_label = (
                "EmergencySeedUsed — ADMIN-SUPPLIED, not from the oracle "
                "(see warning above)"
            )
        print(f"vrf_seed:                {r.seed.hex()}")
        print(f"seed source:             {seed_label}")
        if r.force:
            print(f"vrf force:               {r.force.hex()}")
            print(f"randomness account:      {r.randomness_account}")
            print(f"seed slot:               {r.seed_slot}")
        print(f"deposits aggregated:     {r.n_deposits}")
        print(f"unique mints:            {len(r.weights)}")
        print(f"pool (lamports):         {pool}")
        print(f"fee (lamports):          {r.fee_amount}")
        print(f"budget (lamports):       {budget}")
        print(f"k (draws):               {k}")
        print()
        print("wins:")
        for m, w in sorted(wins.items(), key=lambda x: (-x[1], x[0])):
            if w:
                print(f"  {m}: {w}")
        print()
        print("targets (lamports):")
        for m, t in sorted(targets.items(), key=lambda x: (-x[1], x[0])):
            if t:
                print(f"  {m}: {t}")
        print()
        print(f"VERDICT: {result['verdict']}")

    return 0 if hash_match else 1


if __name__ == "__main__":
    sys.exit(main())
