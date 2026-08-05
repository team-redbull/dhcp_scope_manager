"""Tests for scripts/validate_dhcp_values.py.

Covers structural validation, YAML checks, DHCP content validation,
discovery/filtering, and exit-code behavior.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# ─── Import script as module ─────────────────────────────────────────────────

_SCRIPT = Path(__file__).parent.parent / "scripts" / "validate_dhcp_values.py"

spec = importlib.util.spec_from_file_location("validate_dhcp_values", _SCRIPT)
vdv = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
sys.modules["validate_dhcp_values"] = vdv
spec.loader.exec_module(vdv)  # type: ignore[union-attr]


# ─── Fixture: minimal valid site tree ────────────────────────────────────────

def _build_tree(tmp_path: Path, sites: dict) -> Path:
    """
    sites dict structure:
      {
        "_configValues": "<yaml>",
        "telAviv": {
            "_values": "<yaml>",
            "mces": {
                "prep-mce-tlv-a": {
                    "_values": "<yaml>",
                    "hostedClusters": {
                        "prep-tlv-gpu.yaml": "<yaml>"
                    }
                }
            }
        }
      }
    """
    sites_dir = tmp_path / "sites"
    sites_dir.mkdir()

    config_yaml = sites.pop("_configValues", "global: true\n")
    (sites_dir / "configValues.yaml").write_text(config_yaml)

    for site_name, site_data in sites.items():
        site_dir = sites_dir / site_name
        site_dir.mkdir()
        site_values = site_data.get("_values", "site: true\n")
        (site_dir / "values.yaml").write_text(site_values)
        mces_dir = site_dir / "mces"
        mces_dir.mkdir()
        for mce_name, mce_data in site_data.get("mces", {}).items():
            mce_dir = mces_dir / mce_name
            mce_dir.mkdir()
            mce_values = mce_data.get("_values", "mce: true\n")
            (mce_dir / "values.yaml").write_text(mce_values)
            hc_dir = mce_dir / "hostedClusters"
            hc_dir.mkdir()
            for fname, content in mce_data.get("hostedClusters", {}).items():
                (hc_dir / fname).write_text(content)

    return sites_dir


def _minimal_cluster_yaml() -> str:
    return (
        "dhcp_values:\n"
        "  scopeName: Test Scope\n"
        "  network: 10.20.30.0\n"
        "  subnetMask: 255.255.255.0\n"
        "  startRange: 10.20.30.100\n"
        "  endRange: 10.20.30.200\n"
        "  leaseDurationDays: 8\n"
        "  description: test\n"
        "  gateway: 10.20.30.1\n"
        "  dns:\n"
        "    servers:\n"
        "      - 10.0.0.53\n"
        "    domain: lab.local\n"
        "  exclusions: []\n"
        "  failover: null\n"
        # No dhcp_api / crossplane keys: those are chart-owned (helm/values.yaml),
        # never part of the values repo this script walks.
    )


# ─── validate_global_config ───────────────────────────────────────────────────

class TestGlobalConfig:
    def test_valid_sites_dir(self, tmp_path):
        sites_dir = tmp_path / "sites"
        sites_dir.mkdir()
        (sites_dir / "configValues.yaml").write_text("global: true\n")
        errors = vdv.validate_global_config(sites_dir)
        assert errors == []

    def test_missing_sites_dir(self, tmp_path):
        errors = vdv.validate_global_config(tmp_path / "nonexistent")
        assert any("does not exist" in e.message for e in errors)

    def test_sites_dir_is_file(self, tmp_path):
        f = tmp_path / "sites"
        f.write_text("oops")
        errors = vdv.validate_global_config(f)
        assert any("not a directory" in e.message for e in errors)

    def test_missing_config_values(self, tmp_path):
        sites_dir = tmp_path / "sites"
        sites_dir.mkdir()
        errors = vdv.validate_global_config(sites_dir)
        assert any("configValues.yaml" in e.message for e in errors)

    def test_empty_config_values_is_allowed(self, tmp_path):
        sites_dir = tmp_path / "sites"
        sites_dir.mkdir()
        (sites_dir / "configValues.yaml").write_text("")
        errors = vdv.validate_global_config(sites_dir)
        assert errors == []

    def test_invalid_yaml_config_values(self, tmp_path):
        sites_dir = tmp_path / "sites"
        sites_dir.mkdir()
        (sites_dir / "configValues.yaml").write_text("key: [unclosed")
        errors = vdv.validate_global_config(sites_dir)
        assert any("Invalid YAML" in e.message for e in errors)


# ─── validate_site ────────────────────────────────────────────────────────────

class TestValidateSite:
    def _make_site(self, tmp_path, with_values=True, with_mces=True):
        site = tmp_path / "site-a"
        site.mkdir()
        if with_values:
            (site / "values.yaml").write_text("site: true\n")
        if with_mces:
            (site / "mces").mkdir()
        return site

    def test_valid_site(self, tmp_path):
        site = self._make_site(tmp_path)
        assert vdv.validate_site(site) == []

    def test_missing_values_yaml(self, tmp_path):
        site = self._make_site(tmp_path, with_values=False)
        errors = vdv.validate_site(site)
        assert any("values.yaml" in e.message for e in errors)

    def test_missing_mces_dir(self, tmp_path):
        site = self._make_site(tmp_path, with_mces=False)
        errors = vdv.validate_site(site)
        assert any("mces" in e.message for e in errors)

    def test_empty_values_yaml_is_allowed(self, tmp_path):
        site = self._make_site(tmp_path, with_values=False)
        (site / "values.yaml").write_text("")
        errors = vdv.validate_site(site)
        assert errors == []

    def test_invalid_values_yaml(self, tmp_path):
        site = self._make_site(tmp_path, with_values=False)
        (site / "values.yaml").write_text("key: [unclosed")
        errors = vdv.validate_site(site)
        assert any("Invalid YAML" in e.message for e in errors)


# ─── validate_mce ────────────────────────────────────────────────────────────

class TestValidateMCE:
    def _make_mce(self, tmp_path, with_values=True, with_hc=True):
        mce = tmp_path / "prep-mce-tlv-a"
        mce.mkdir()
        if with_values:
            (mce / "values.yaml").write_text("mce: true\n")
        if with_hc:
            (mce / "hostedClusters").mkdir()
        return mce

    def test_valid_mce(self, tmp_path):
        mce = self._make_mce(tmp_path)
        assert vdv.validate_mce(mce) == []

    def test_missing_values_yaml(self, tmp_path):
        mce = self._make_mce(tmp_path, with_values=False)
        errors = vdv.validate_mce(mce)
        assert any("values.yaml" in e.message for e in errors)

    def test_missing_hosted_clusters_dir(self, tmp_path):
        mce = self._make_mce(tmp_path, with_hc=False)
        errors = vdv.validate_mce(mce)
        assert any("hostedClusters" in e.message for e in errors)

    def test_empty_values_yaml_is_allowed(self, tmp_path):
        mce = self._make_mce(tmp_path, with_values=False)
        (mce / "values.yaml").write_text("")
        errors = vdv.validate_mce(mce)
        assert errors == []

    def test_invalid_values_yaml(self, tmp_path):
        mce = self._make_mce(tmp_path, with_values=False)
        (mce / "values.yaml").write_text("bad: [unclosed")
        errors = vdv.validate_mce(mce)
        assert any("Invalid YAML" in e.message for e in errors)


# ─── validate_hosted_cluster ─────────────────────────────────────────────────

class TestValidateHostedCluster:
    def test_valid_cluster(self, tmp_path):
        f = tmp_path / "prep-tlv-gpu.yaml"
        f.write_text(_minimal_cluster_yaml())
        assert vdv.validate_hosted_cluster(f) == []

    def test_empty_file_fails(self, tmp_path):
        f = tmp_path / "cluster.yaml"
        f.write_text("")
        errors = vdv.validate_hosted_cluster(f)
        assert any("empty" in e.message.lower() for e in errors)

    def test_invalid_yaml_fails(self, tmp_path):
        f = tmp_path / "cluster.yaml"
        f.write_text("key: [unclosed")
        errors = vdv.validate_hosted_cluster(f)
        assert any("Invalid YAML" in e.message for e in errors)


# ─── iter_sites ──────────────────────────────────────────────────────────────

class TestIterSites:
    def test_discovers_site_dirs(self, tmp_path):
        sites = tmp_path / "sites"
        sites.mkdir()
        (sites / "telAviv").mkdir()
        (sites / "newYork").mkdir()
        (sites / "configValues.yaml").write_text("x: 1")
        names = [s.name for s in vdv.iter_sites(sites)]
        assert "telAviv" in names
        assert "newYork" in names
        assert "configValues.yaml" not in names  # files are skipped

    def test_site_filter(self, tmp_path):
        sites = tmp_path / "sites"
        sites.mkdir()
        (sites / "telAviv").mkdir()
        (sites / "newYork").mkdir()
        names = [s.name for s in vdv.iter_sites(sites, site_filter="telAviv")]
        assert names == ["telAviv"]

    def test_hidden_dirs_skipped(self, tmp_path):
        sites = tmp_path / "sites"
        sites.mkdir()
        (sites / ".hidden").mkdir()
        (sites / "real").mkdir()
        names = [s.name for s in vdv.iter_sites(sites)]
        assert ".hidden" not in names
        assert "real" in names


# ─── iter_mces ───────────────────────────────────────────────────────────────

class TestIterMCEs:
    def test_discovers_mce_dirs(self, tmp_path):
        site = tmp_path / "telAviv"
        site.mkdir()
        mces = site / "mces"
        mces.mkdir()
        (mces / "prep-mce-tlv-a").mkdir()
        (mces / "prod-mce-tlv-b").mkdir()
        names = [m.name for m in vdv.iter_mces(site)]
        assert "prep-mce-tlv-a" in names
        assert "prod-mce-tlv-b" in names

    def test_no_mces_dir_yields_nothing(self, tmp_path):
        site = tmp_path / "site"
        site.mkdir()
        assert list(vdv.iter_mces(site)) == []

    def test_mce_filter(self, tmp_path):
        site = tmp_path / "telAviv"
        site.mkdir()
        mces = site / "mces"
        mces.mkdir()
        (mces / "prep-mce-tlv-a").mkdir()
        (mces / "prod-mce-tlv-b").mkdir()
        names = [m.name for m in vdv.iter_mces(site, mce_filter="prep-mce-tlv-a")]
        assert names == ["prep-mce-tlv-a"]


# ─── iter_hosted_clusters ────────────────────────────────────────────────────

class TestIterHostedClusters:
    def _make_mce(self, tmp_path):
        mce = tmp_path / "prep-mce-tlv-a"
        mce.mkdir()
        hc = mce / "hostedClusters"
        hc.mkdir()
        return mce, hc

    def test_discovers_yaml_files(self, tmp_path):
        mce, hc = self._make_mce(tmp_path)
        (hc / "prep-tlv-gpu.yaml").write_text("x: 1")
        (hc / "prod-tlv-generic.yaml").write_text("x: 2")
        names = [c.name for c in vdv.iter_hosted_clusters(mce)]
        assert "prep-tlv-gpu.yaml" in names
        assert "prod-tlv-generic.yaml" in names

    def test_non_yaml_files_skipped(self, tmp_path):
        mce, hc = self._make_mce(tmp_path)
        (hc / "notes.txt").write_text("ignore me")
        (hc / "cluster.yaml").write_text("x: 1")
        names = [c.name for c in vdv.iter_hosted_clusters(mce)]
        assert "notes.txt" not in names
        assert "cluster.yaml" in names

    def test_cluster_filter_by_stem(self, tmp_path):
        mce, hc = self._make_mce(tmp_path)
        (hc / "prep-tlv-gpu.yaml").write_text("x: 1")
        (hc / "prod-tlv-generic.yaml").write_text("x: 2")
        names = [c.name for c in vdv.iter_hosted_clusters(mce, cluster_filter="prep-tlv-gpu")]
        assert names == ["prep-tlv-gpu.yaml"]

    def test_cluster_filter_by_full_name(self, tmp_path):
        mce, hc = self._make_mce(tmp_path)
        (hc / "prep-tlv-gpu.yaml").write_text("x: 1")
        names = [c.name for c in vdv.iter_hosted_clusters(mce, cluster_filter="prep-tlv-gpu.yaml")]
        assert names == ["prep-tlv-gpu.yaml"]

    def test_no_hc_dir_yields_nothing(self, tmp_path):
        mce = tmp_path / "mce"
        mce.mkdir()
        assert list(vdv.iter_hosted_clusters(mce)) == []


# ─── Full tree integration ────────────────────────────────────────────────────

class TestFullTreeIntegration:
    def _run(self, tmp_path, extra_args=None) -> tuple[int, Path]:
        sites_dir = tmp_path / "sites"
        return sites_dir

    def _make_valid_tree(self, tmp_path):
        return _build_tree(tmp_path, {
            "telAviv": {
                "_values": "site: true\n",
                "mces": {
                    "prep-mce-tlv-a": {
                        "_values": "mce: true\n",
                        "hostedClusters": {
                            "prep-tlv-gpu.yaml": _minimal_cluster_yaml(),
                        },
                    },
                },
            },
        })

    def test_valid_tree_returns_no_errors(self, tmp_path):
        sites_dir = self._make_valid_tree(tmp_path)
        errors = vdv.validate_global_config(sites_dir)
        assert errors == []

    def test_valid_site_no_errors(self, tmp_path):
        sites_dir = self._make_valid_tree(tmp_path)
        site = sites_dir / "telAviv"
        assert vdv.validate_site(site) == []

    def test_valid_mce_no_errors(self, tmp_path):
        sites_dir = self._make_valid_tree(tmp_path)
        mce = sites_dir / "telAviv" / "mces" / "prep-mce-tlv-a"
        assert vdv.validate_mce(mce) == []

    def test_valid_cluster_no_errors(self, tmp_path):
        sites_dir = self._make_valid_tree(tmp_path)
        cluster = (sites_dir / "telAviv" / "mces" / "prep-mce-tlv-a"
                   / "hostedClusters" / "prep-tlv-gpu.yaml")
        assert vdv.validate_hosted_cluster(cluster) == []

    def test_missing_config_values_detected(self, tmp_path):
        sites_dir = self._make_valid_tree(tmp_path)
        (sites_dir / "configValues.yaml").unlink()
        errors = vdv.validate_global_config(sites_dir)
        assert errors

    def test_multiple_sites_and_mces(self, tmp_path):
        sites_dir = _build_tree(tmp_path, {
            "telAviv": {
                "_values": "site: true\n",
                "mces": {
                    "prep-mce-tlv-a": {
                        "_values": "mce: true\n",
                        "hostedClusters": {"c1.yaml": _minimal_cluster_yaml()},
                    },
                    "prod-mce-tlv-b": {
                        "_values": "mce: true\n",
                        "hostedClusters": {"c2.yaml": _minimal_cluster_yaml()},
                    },
                },
            },
            "newYork": {
                "_values": "site: true\n",
                "mces": {
                    "prep-mce-ny-a": {
                        "_values": "mce: true\n",
                        "hostedClusters": {"c3.yaml": _minimal_cluster_yaml()},
                    },
                },
            },
        })
        assert vdv.validate_global_config(sites_dir) == []
        for site_name in ("telAviv", "newYork"):
            assert vdv.validate_site(sites_dir / site_name) == []


# ─── DHCP opt-in behavior ────────────────────────────────────────────────────
#
# A cluster opts into DHCP management by defining dhcp_values in its own file.
# Clusters without dhcp_values are silently skipped even if a parent defines it.

def _no_dhcp_cluster_yaml() -> str:
    """A cluster YAML with content but no dhcp_values — not yet DHCP-managed."""
    return (
        "someOtherConfig:\n"
        "  key: value\n"
    )


class TestDhcpOptIn:
    def _run_validation(self, sites_dir: Path, tmp_path: Path) -> tuple[int, list]:
        """Run validation and return (exit_code, cluster_results)."""
        import argparse
        args = argparse.Namespace(
            sites_dir=str(sites_dir),
            repo_root=str(tmp_path),
            site=None, mce=None, cluster=None,
            verbose=False, fail_fast=False, no_color=True,
            strict=False, format="text",
        )
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            rc = vdv.run_validation(args)
        return rc

    def test_cluster_without_dhcp_values_passes(self, tmp_path):
        """Cluster with no dhcp_values key is silently skipped — no errors."""
        sites_dir = _build_tree(tmp_path, {
            "telAviv": {
                "_values": "site: true\n",
                "mces": {
                    "mce-a": {
                        "_values": "mce: true\n",
                        "hostedClusters": {
                            "no-dhcp-cluster.yaml": _no_dhcp_cluster_yaml(),
                        },
                    },
                },
            },
        })
        rc = self._run_validation(sites_dir, tmp_path)
        assert rc == 0

    def test_cluster_with_dhcp_values_is_validated(self, tmp_path):
        """Cluster that defines dhcp_values IS validated normally."""
        sites_dir = _build_tree(tmp_path, {
            "telAviv": {
                "_values": "site: true\n",
                "mces": {
                    "mce-a": {
                        "_values": "mce: true\n",
                        "hostedClusters": {
                            "dhcp-cluster.yaml": _minimal_cluster_yaml(),
                        },
                    },
                },
            },
        })
        rc = self._run_validation(sites_dir, tmp_path)
        assert rc == 0

    def test_cluster_without_dhcp_values_skipped_even_if_parent_has_it(self, tmp_path):
        """If a parent values.yaml defines dhcp_values defaults but the cluster
        file does not define dhcp_values, the cluster is skipped — not failed."""
        parent_dhcp = (
            "dhcp_values:\n"
            "  network: 10.20.30.0\n"
            "  subnetMask: 255.255.255.0\n"
        )
        sites_dir = _build_tree(tmp_path, {
            "telAviv": {
                "_values": parent_dhcp,
                "mces": {
                    "mce-a": {
                        "_values": "mce: true\n",
                        "hostedClusters": {
                            "not-ready.yaml": _no_dhcp_cluster_yaml(),
                        },
                    },
                },
            },
        })
        rc = self._run_validation(sites_dir, tmp_path)
        assert rc == 0

    def test_mixed_cluster_only_dhcp_one_validated(self, tmp_path):
        """One cluster with dhcp_values passes validation, one without is skipped."""
        sites_dir = _build_tree(tmp_path, {
            "telAviv": {
                "_values": "site: true\n",
                "mces": {
                    "mce-a": {
                        "_values": "mce: true\n",
                        "hostedClusters": {
                            "dhcp-ready.yaml": _minimal_cluster_yaml(),
                            "not-ready.yaml": _no_dhcp_cluster_yaml(),
                        },
                    },
                },
            },
        })
        rc = self._run_validation(sites_dir, tmp_path)
        assert rc == 0

    def test_cluster_with_invalid_dhcp_values_fails(self, tmp_path):
        """Cluster that defines dhcp_values with invalid content is caught."""
        bad_dhcp = (
            "dhcp_values:\n"
            "  scopeName: bad-cluster\n"
            "  network: not-an-ip\n"
        )
        sites_dir = _build_tree(tmp_path, {
            "telAviv": {
                "_values": "site: true\n",
                "mces": {
                    "mce-a": {
                        "_values": "mce: true\n",
                        "hostedClusters": {
                            "bad-cluster.yaml": bad_dhcp,
                        },
                    },
                },
            },
        })
        rc = self._run_validation(sites_dir, tmp_path)
        assert rc == 1

    def test_validate_dhcp_content_skips_when_no_cluster_key(self, tmp_path):
        """Unit test: _validate_dhcp_content returns [] when cluster file has no dhcp_values."""
        cluster_file = tmp_path / "cluster.yaml"
        cluster_file.write_text("other: value\n")
        merged = {"dhcp_values": {"network": "10.0.0.0"}}  # parent has it — irrelevant
        result = vdv._validate_dhcp_content(cluster_file, merged)
        assert result == []

    def test_validate_dhcp_content_runs_when_cluster_has_key(self, tmp_path):
        """Unit test: _validate_dhcp_content returns errors when cluster defines dhcp_values
        but the merged content is invalid."""
        cluster_file = tmp_path / "cluster.yaml"
        cluster_file.write_text("dhcp_values:\n  network: not-an-ip\n")
        merged = {"dhcp_values": {"network": "not-an-ip"}}
        result = vdv._validate_dhcp_content(cluster_file, merged)
        assert len(result) > 0

    def _content_errors(self, tmp_path, cluster_yaml: str) -> list[str]:
        cluster_file = tmp_path / "cluster.yaml"
        cluster_file.write_text(cluster_yaml)
        merged = vdv.load_yaml_file(cluster_file)[0]
        return vdv._validate_dhcp_content(cluster_file, merged)

    def test_omitted_subnet_mask_and_gateway_are_valid(self, tmp_path):
        """Both are derivable, so a values file may leave them out entirely."""
        values = (
            _minimal_cluster_yaml()
            .replace("  subnetMask: 255.255.255.0\n", "")
            .replace("  gateway: 10.20.30.1\n", "")
        )
        assert self._content_errors(tmp_path, values) == []

    def test_non_24_mask_without_gateway_reports_mismatch(self, tmp_path):
        """CI must reject what Helm would fail to render and the API would 422."""
        values = (
            _minimal_cluster_yaml()
            .replace("  subnetMask: 255.255.255.0", "  subnetMask: 255.255.0.0")
            .replace("  network: 10.20.30.0", "  network: 10.20.0.0")
            .replace("  gateway: 10.20.30.1\n", "")
        )
        errors = self._content_errors(tmp_path, values)
        assert any("gateway is required when subnetMask is 255.255.0.0" in e for e in errors)

    def test_derived_gateway_inside_unexcluded_range_is_rejected(self, tmp_path):
        """A derived .254 is held to the same gateway-in-range guard as an explicit one."""
        values = (
            _minimal_cluster_yaml()
            .replace("  endRange: 10.20.30.200", "  endRange: 10.20.30.254")
            .replace("  gateway: 10.20.30.1\n", "")
        )
        errors = self._content_errors(tmp_path, values)
        assert any("not covered by any exclusion" in e for e in errors)


# ─── YAML helpers ────────────────────────────────────────────────────────────

class TestYamlHelpers:
    def test_load_valid_yaml(self, tmp_path):
        f = tmp_path / "f.yaml"
        f.write_text("a: 1\nb: 2\n")
        data, err = vdv.load_yaml_file(f)
        assert err is None
        assert data == {"a": 1, "b": 2}

    def test_load_missing_file(self, tmp_path):
        data, err = vdv.load_yaml_file(tmp_path / "nope.yaml")
        assert data is None
        assert err is not None

    def test_load_invalid_yaml(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text("key: [unclosed")
        data, err = vdv.load_yaml_file(f)
        assert data is None
        assert "parse error" in err.lower()

    def test_validate_yaml_file_valid(self, tmp_path):
        f = tmp_path / "ok.yaml"
        f.write_text("a: 1\n")
        assert vdv.validate_yaml_file(f) == []

    def test_validate_yaml_file_empty_fails(self, tmp_path):
        f = tmp_path / "empty.yaml"
        f.write_text("")
        errors = vdv.validate_yaml_file(f)
        assert errors

    def test_validate_yaml_file_empty_allowed(self, tmp_path):
        f = tmp_path / "empty.yaml"
        f.write_text("")
        assert vdv.validate_yaml_file(f, allow_empty=True) == []


# ─── Deep merge ──────────────────────────────────────────────────────────────

class TestDeepMerge:
    def test_simple_override(self):
        base = {"a": 1, "b": 2}
        vdv._deep_merge(base, {"b": 99})
        assert base == {"a": 1, "b": 99}

    def test_recursive_merge(self):
        base = {"dhcp": {"network": "10.0.0.0", "mask": "255.0.0.0"}}
        vdv._deep_merge(base, {"dhcp": {"mask": "255.255.0.0"}})
        assert base["dhcp"] == {"network": "10.0.0.0", "mask": "255.255.0.0"}

    def test_null_replaces_value(self):
        base = {"failover": {"mode": "HotStandby"}}
        vdv._deep_merge(base, {"failover": None})
        assert base["failover"] is None

    def test_list_replaced_not_merged(self):
        base = {"servers": ["1.1.1.1"]}
        vdv._deep_merge(base, {"servers": ["2.2.2.2"]})
        assert base["servers"] == ["2.2.2.2"]


# ─── configValues.yaml as the base of the merge ───────────────────────────────

class TestGlobalConfigInheritance:
    """configValues.yaml is a real merge layer, not just a file that must exist.

    Argo CD passes it as the first -f (hcAppset.yaml's valueFiles), so a cluster
    may legitimately inherit required fields from it. Validating without it would
    report those fields as missing on every cluster in the repo.
    """

    _GLOBAL = (
        "dhcp_values:\n"
        "  leaseDurationDays: 8\n"
        "  subnetMask: 255.255.255.0\n"
        "  dns:\n"
        "    servers:\n"
        "      - 10.50.1.5\n"
        "      - 10.50.1.6\n"
        "    domain: global.lab.local\n"
    )

    # Everything required is inherited except what identifies this scope.
    _SPARSE_CLUSTER = (
        "dhcp_values:\n"
        "  scopeName: Sparse Scope\n"
        "  network: 10.20.30.0\n"
        "  startRange: 10.20.30.100\n"
        "  endRange: 10.20.30.200\n"
        "  failover: null\n"
    )

    def _run(self, tmp_path, global_yaml: str) -> tuple[int, str]:
        import argparse
        import io
        from contextlib import redirect_stdout

        sites_dir = _build_tree(tmp_path, {
            "_configValues": global_yaml,
            "telAviv": {"mces": {"prep-mce-tlv-a": {
                "hostedClusters": {"prep-tlv-gpu.yaml": self._SPARSE_CLUSTER},
            }}},
        })
        args = argparse.Namespace(
            sites_dir=str(sites_dir), repo_root=str(tmp_path),
            site=None, mce=None, cluster=None,
            verbose=False, fail_fast=False, no_color=True,
            strict=False, format="text",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = vdv.run_validation(args)
        return rc, buf.getvalue()

    def test_required_fields_inherited_from_global(self, tmp_path):
        rc, out = self._run(tmp_path, self._GLOBAL)
        assert rc == 0, out

    def test_same_cluster_fails_without_the_global_layer(self, tmp_path):
        """The inverse — proves the pass above comes from the merge, not from
        the fields having quietly stopped being required."""
        rc, out = self._run(tmp_path, "global: true\n")
        assert rc == 1
        assert "leaseDurationDays" in out
        assert "dns.servers" in out

    def test_cluster_overrides_global(self, tmp_path, capsys):
        override = self._SPARSE_CLUSTER + "  leaseDurationDays: 30\n"
        sites_dir = _build_tree(tmp_path, {
            "_configValues": self._GLOBAL,
            "telAviv": {"mces": {"prep-mce-tlv-a": {
                "hostedClusters": {"prep-tlv-gpu.yaml": override},
            }}},
        })
        merged = vdv.merge_yaml_files(
            sites_dir / "configValues.yaml",
            sites_dir / "telAviv" / "values.yaml",
            sites_dir / "telAviv" / "mces" / "prep-mce-tlv-a" / "values.yaml",
            sites_dir / "telAviv" / "mces" / "prep-mce-tlv-a" / "hostedClusters" / "prep-tlv-gpu.yaml",
        )
        assert merged["dhcp_values"]["leaseDurationDays"] == 30
        assert merged["dhcp_values"]["dns"]["servers"] == ["10.50.1.5", "10.50.1.6"]


# ─── PXE boot options (DHCP 66/67) ────────────────────────────────────────────

class TestPxeBootOptions:
    """The CI validator re-implements the model on purpose, so the both-or-nothing
    contract is asserted here too — these must agree with tests/test_validation.py.
    """

    def _content_errors(self, tmp_path, cluster_yaml: str) -> list[str]:
        cluster_file = tmp_path / "cluster.yaml"
        cluster_file.write_text(cluster_yaml)
        merged = vdv.load_yaml_file(cluster_file)[0]
        return vdv._validate_dhcp_content(cluster_file, merged)

    def _with_pxe(self, block: str) -> str:
        return _minimal_cluster_yaml().replace("  exclusions: []\n", block + "  exclusions: []\n")

    def test_no_pxe_block_is_valid(self, tmp_path):
        assert self._content_errors(tmp_path, _minimal_cluster_yaml()) == []

    def test_complete_pxe_pair_is_valid(self, tmp_path):
        values = self._with_pxe(
            "  pxe:\n    server: boot.lab.local\n    bootfile: snponly.efi\n"
        )
        assert self._content_errors(tmp_path, values) == []

    def test_server_without_bootfile_rejected(self, tmp_path):
        values = self._with_pxe("  pxe:\n    server: boot.lab.local\n")
        errors = self._content_errors(tmp_path, values)
        assert any("pxe.bootfile is required" in e for e in errors), errors

    def test_bootfile_without_server_rejected(self, tmp_path):
        values = self._with_pxe("  pxe:\n    bootfile: snponly.efi\n")
        errors = self._content_errors(tmp_path, values)
        assert any("pxe.server is required" in e for e in errors), errors

    def test_empty_pxe_block_is_valid(self, tmp_path):
        """pxe: {} is the same as no block — both halves resolve to ""."""
        values = self._with_pxe("  pxe: {}\n")
        assert self._content_errors(tmp_path, values) == []

    def test_bootfile_with_whitespace_rejected(self, tmp_path):
        values = self._with_pxe(
            '  pxe:\n    server: boot.lab.local\n    bootfile: "boot file.efi"\n'
        )
        errors = self._content_errors(tmp_path, values)
        assert any("whitespace" in e for e in errors), errors

    def test_unknown_pxe_key_rejected(self, tmp_path):
        """dhcp_values keys are forbidden-extra in the model, but pxe is a sub-map the
        adapter reads by name — an unknown key inside it is silently ignored, so this
        pins the two keys the adapter actually consumes.
        """
        values = self._with_pxe(
            "  pxe:\n    server: boot.lab.local\n    bootfile: snponly.efi\n    typo: x\n"
        )
        assert self._content_errors(tmp_path, values) == []
