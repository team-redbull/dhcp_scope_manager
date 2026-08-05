#!/usr/bin/env python3
"""Sync the DHCP scope templates into a consuming Helm chart.

The Crossplane Request CR is rendered by whichever chart the GitOps cascade
deploys per hosted cluster (`hostedclusters-setup`), not by a chart of its own —
that is how one values file can create both the cluster and its DHCP scope.

But this repo stays the source of truth: it holds the test suite that renders
these templates, including the parity test asserting the rendered body equals
what the API's GET returns. So the templates are copied downstream and the copies
are checked, rather than being maintained in two places.

    python3 scripts/sync_chart.py --target ../helm-charts-hostedclusters-setup
    python3 scripts/sync_chart.py --target ../helm-charts-hostedclusters-setup --check

`--check` is the CI mode: exit 1 if the target has drifted. Run it in the
consuming chart's pipeline so drift fails a build, rather than surfacing months
later as a mis-rendered CR.

Deliberately does NOT write the target's values.yaml — that file is owned by the
other chart. It is only checked for the keys the templates require, since a chart
that has the templates but not `dhcp_api.url` fails at render time with a message
that does not obviously point here.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_TEMPLATES = REPO_ROOT / "helm" / "templates"

# The two files that make up the DHCP integration. _dhcp-helpers.tpl defines
# dhcp.payload and dhcp.defaultGateway; the CR template is the only consumer.
SYNCED_FILES = ("dhcp-scope-request.yaml", "_dhcp-helpers.tpl")

# Keys the templates read from the consuming chart's values.yaml. dhcp_values is
# absent on purpose — it comes from the values repo, per hosted cluster.
REQUIRED_VALUES_KEYS = (
    ("dhcp_api", "url"),
    ("crossplane", "namespace"),
    ("crossplane", "providerConfigName"),
)

HEADER = """\
# GENERATED — DO NOT EDIT.
#
# Synced from team-redbull/dhcp_scope_manager, helm/templates/{name}.
# Edit it there and re-run `scripts/sync_chart.py --target <this chart>`;
# changes made here are silently reverted on the next sync.
"""

VALUES_SNIPPET = """\
crossplane:
  namespace: crossplane-system
  providerConfigName: dhcp-http

dhcp_api:
  url: http://dhcp-scope-manager.dhcp-scope-manager.svc.cluster.local:8080
  tokenSecretRef: {}
"""


def rendered(name: str) -> str:
    """Source file with the generated-by header prepended."""
    body = (SOURCE_TEMPLATES / name).read_text()
    return HEADER.format(name=name) + body


def check_values(target: Path) -> list[str]:
    """Report required keys the target's values.yaml does not resolve."""
    values_path = target / "values.yaml"
    if not values_path.exists():
        return [f"{values_path} does not exist"]

    data = yaml.safe_load(values_path.read_text()) or {}
    problems = []
    for keys in REQUIRED_VALUES_KEYS:
        node = data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        # An explicit empty string is as broken as a missing key: the chart's
        # `required` guard fires on both.
        if node is None or node == "":
            problems.append(".".join(keys))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", required=True, type=Path,
                        help="Path to the consuming chart (the dir holding Chart.yaml)")
    parser.add_argument("--check", action="store_true",
                        help="Verify only; exit 1 on drift. Does not write.")
    args = parser.parse_args()

    target = args.target.resolve()
    target_templates = target / "templates"

    if not (target / "Chart.yaml").exists():
        print(f"error: {target} has no Chart.yaml — not a Helm chart", file=sys.stderr)
        return 2

    drifted: list[str] = []
    for name in SYNCED_FILES:
        want = rendered(name)
        dest = target_templates / name

        if args.check:
            if not dest.exists():
                drifted.append(f"{name}: missing from target")
            elif dest.read_text() != want:
                drifted.append(f"{name}: differs from source")
        else:
            target_templates.mkdir(parents=True, exist_ok=True)
            dest.write_text(want)
            print(f"synced  templates/{name}")

    value_problems = check_values(target)

    if args.check:
        if drifted:
            print("Chart templates have drifted from dhcp_scope_manager:", file=sys.stderr)
            for d in drifted:
                print(f"  {d}", file=sys.stderr)
            print("\nRe-sync with:  python3 scripts/sync_chart.py --target "
                  f"{args.target}", file=sys.stderr)
        if value_problems:
            print(f"\n{target / 'values.yaml'} is missing keys the templates require:",
                  file=sys.stderr)
            for p in value_problems:
                print(f"  {p}", file=sys.stderr)
            print(f"\nAdd:\n\n{VALUES_SNIPPET}", file=sys.stderr)
        if drifted or value_problems:
            return 1
        print("chart is in sync")
        return 0

    if value_problems:
        # A warning, not a failure: the copy succeeded, and values.yaml belongs to
        # the other chart. Rendering will fail until these are added, so say so.
        print(f"\nWARNING: {target / 'values.yaml'} does not resolve: "
              f"{', '.join(value_problems)}", file=sys.stderr)
        print(f"The chart will fail to render until it does. Add:\n\n{VALUES_SNIPPET}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
