#!/usr/bin/env python3
"""Validate only DHCP clusters affected by the current git diff.

Inheritance-aware: a change to a parent values.yaml triggers validation
of every cluster that inherits from it.

  sites/configValues.yaml                         → full repo scan
  sites/<site>/values.yaml                        → all clusters under <site>
  sites/<site>/mces/<mce>/values.yaml             → all clusters under <mce>
  sites/<site>/mces/<mce>/hostedClusters/<c>.yaml → only <c>

Usage:
  python3 scripts/validate_changed_clusters.py
  python3 scripts/validate_changed_clusters.py --base-ref origin/main
  python3 scripts/validate_changed_clusters.py --verbose

Exit codes: 0 = pass, 1 = validation errors, 2 = script/git error
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class _Target:
    site: Optional[str]     # None = all sites
    mce: Optional[str]      # None = all MCEs under site
    cluster: Optional[str]  # None = all clusters under MCE

    def label(self) -> str:
        if self.mce is None:
            return f"site={self.site} (all clusters)"
        if self.cluster is None:
            return f"site={self.site}, mce={self.mce} (all clusters)"
        return f"site={self.site}, mce={self.mce}, cluster={self.cluster}"


def _get_changed_files(base_ref: str, repo_root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=repo_root,
        )
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: git diff failed: {exc.stderr.strip()}", file=sys.stderr)
        sys.exit(2)
    return [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]


def _resolve_targets(changed: list[Path], sites_dir: str) -> list[_Target] | None:
    """
    Map changed file paths to validation targets.
    Returns None to signal a full-repo scan is needed.
    Returns [] if no sites/ files were touched.
    """
    targets: set[_Target] = set()

    for f in changed:
        parts = f.parts

        if not parts or parts[0] != sites_dir:
            continue

        # sites/configValues.yaml → full scan
        if len(parts) == 2 and parts[1] == "configValues.yaml":
            return None

        if len(parts) < 3:
            continue

        site = parts[1]

        # sites/<site>/values.yaml → all clusters under site
        if len(parts) == 3 and parts[2] == "values.yaml":
            targets.add(_Target(site=site, mce=None, cluster=None))
            continue

        # Must be under sites/<site>/mces/
        if len(parts) < 5 or parts[2] != "mces":
            continue

        mce = parts[3]

        # sites/<site>/mces/<mce>/values.yaml → all clusters under MCE
        if len(parts) == 5 and parts[4] == "values.yaml":
            targets.add(_Target(site=site, mce=mce, cluster=None))
            continue

        # sites/<site>/mces/<mce>/hostedClusters/<cluster>.yaml → single cluster
        if len(parts) == 6 and parts[4] == "hostedClusters":
            stem = Path(parts[5]).stem
            if Path(parts[5]).suffix in (".yaml", ".yml"):
                targets.add(_Target(site=site, mce=mce, cluster=stem))

    # Drop targets already covered by a broader one in the same batch
    normalized: set[_Target] = set()
    for t in targets:
        site_wide = _Target(site=t.site, mce=None, cluster=None)
        mce_wide  = _Target(site=t.site, mce=t.mce, cluster=None)
        if t.mce is not None and site_wide in targets:
            continue
        if t.cluster is not None and mce_wide in targets:
            continue
        normalized.add(t)

    return sorted(normalized, key=lambda x: (x.site or "", x.mce or "", x.cluster or ""))


def _make_args(base: argparse.Namespace, target: _Target) -> argparse.Namespace:
    import copy
    a = copy.copy(base)
    a.site    = target.site
    a.mce     = target.mce
    a.cluster = target.cluster
    a.strict  = False
    a.format  = "text"
    return a


def main() -> None:
    p = argparse.ArgumentParser(
        prog="validate_changed_clusters.py",
        description="Validate only DHCP clusters affected by the current git diff.",
    )
    p.add_argument("--base-ref",  default="origin/main",
                   help="Git ref to diff against (default: origin/main)")
    p.add_argument("--sites-dir", default="sites",
                   help="Path to sites/ directory relative to repo root (default: sites)")
    p.add_argument("--repo-root", default=".",
                   help="Repository root (default: current directory)")
    p.add_argument("--verbose",   action="store_true")
    p.add_argument("--fail-fast", action="store_true")
    p.add_argument("--no-color",  action="store_true")
    args = p.parse_args()

    repo_root = Path(args.repo_root).resolve()

    sys.path.insert(0, str(Path(__file__).parent))
    from validate_dhcp_values import run_validation  # noqa: PLC0415

    changed = _get_changed_files(args.base_ref, repo_root)
    if not changed:
        print("No changed files detected. Nothing to validate.")
        sys.exit(0)

    targets = _resolve_targets(changed, args.sites_dir)

    if targets is None:
        print("configValues.yaml changed — running full repo validation.\n")
        full = _make_args(args, _Target(site=None, mce=None, cluster=None))
        sys.exit(run_validation(full))

    if not targets:
        print("No sites/ files changed. Nothing to validate.")
        sys.exit(0)

    print(f"Validating {len(targets)} target(s) from diff against {args.base_ref}:")
    for t in targets:
        print(f"  → {t.label()}")
    print()

    overall = 0
    for t in targets:
        rc = run_validation(_make_args(args, t))
        if rc != 0:
            overall = rc
            if args.fail_fast:
                sys.exit(overall)

    sys.exit(overall)


if __name__ == "__main__":
    main()
