"""Tests for scripts/validate_changed_clusters.py.

Covers target resolution logic, git integration (mocked), and end-to-end
main() behavior using a real tmp_path site tree.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ─── Import script as module ─────────────────────────────────────────────────

_SCRIPT = Path(__file__).parent.parent / "scripts" / "validate_changed_clusters.py"

spec = importlib.util.spec_from_file_location("validate_changed_clusters", _SCRIPT)
vcc = importlib.util.module_from_spec(spec)   # type: ignore[arg-type]
sys.modules["validate_changed_clusters"] = vcc
spec.loader.exec_module(vcc)  # type: ignore[union-attr]

_T = vcc._Target
resolve = vcc._resolve_targets


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _paths(*strings: str) -> list[Path]:
    return [Path(s) for s in strings]


def _cluster(site: str, mce: str, cluster: str) -> _T:
    return _T(site=site, mce=mce, cluster=cluster)


def _mce_wide(site: str, mce: str) -> _T:
    return _T(site=site, mce=mce, cluster=None)


def _site_wide(site: str) -> _T:
    return _T(site=site, mce=None, cluster=None)


def _full_scan() -> None:
    return None


# ─── _resolve_targets: single file changes ───────────────────────────────────

class TestResolveTargetsSingle:
    def test_hosted_cluster_yaml(self):
        result = resolve(
            _paths("sites/telAviv/mces/prep-mce-tlv-a/hostedClusters/tel-aviv-prod.yaml"),
            "sites",
        )
        assert result == [_cluster("telAviv", "prep-mce-tlv-a", "tel-aviv-prod")]

    def test_hosted_cluster_yml_extension(self):
        result = resolve(
            _paths("sites/telAviv/mces/prep-mce-tlv-a/hostedClusters/tel-aviv-prod.yml"),
            "sites",
        )
        assert result == [_cluster("telAviv", "prep-mce-tlv-a", "tel-aviv-prod")]

    def test_mce_values_yaml(self):
        result = resolve(
            _paths("sites/telAviv/mces/prep-mce-tlv-a/values.yaml"),
            "sites",
        )
        assert result == [_mce_wide("telAviv", "prep-mce-tlv-a")]

    def test_site_values_yaml(self):
        result = resolve(
            _paths("sites/telAviv/values.yaml"),
            "sites",
        )
        assert result == [_site_wide("telAviv")]

    def test_config_values_returns_none(self):
        result = resolve(_paths("sites/configValues.yaml"), "sites")
        assert result is None

    def test_non_sites_file_ignored(self):
        result = resolve(_paths("app/routers/scopes.py"), "sites")
        assert result == []

    def test_empty_changed_list(self):
        result = resolve([], "sites")
        assert result == []

    def test_only_non_sites_files(self):
        result = resolve(
            _paths("app/routers/scopes.py", "tests/test_endpoints.py", "README.md"),
            "sites",
        )
        assert result == []


# ─── _resolve_targets: deduplication ─────────────────────────────────────────

class TestResolveTargetsDeduplication:
    def test_site_wide_covers_cluster_under_it(self):
        result = resolve(
            _paths(
                "sites/telAviv/values.yaml",
                "sites/telAviv/mces/prep-mce-tlv-a/hostedClusters/tel-aviv-prod.yaml",
            ),
            "sites",
        )
        assert result == [_site_wide("telAviv")]

    def test_site_wide_covers_mce_wide_under_it(self):
        result = resolve(
            _paths(
                "sites/telAviv/values.yaml",
                "sites/telAviv/mces/prep-mce-tlv-a/values.yaml",
            ),
            "sites",
        )
        assert result == [_site_wide("telAviv")]

    def test_mce_wide_covers_cluster_under_it(self):
        result = resolve(
            _paths(
                "sites/telAviv/mces/prep-mce-tlv-a/values.yaml",
                "sites/telAviv/mces/prep-mce-tlv-a/hostedClusters/tel-aviv-prod.yaml",
            ),
            "sites",
        )
        assert result == [_mce_wide("telAviv", "prep-mce-tlv-a")]

    def test_mce_wide_does_not_cover_cluster_in_different_mce(self):
        result = resolve(
            _paths(
                "sites/telAviv/mces/prep-mce-tlv-a/values.yaml",
                "sites/telAviv/mces/prep-mce-tlv-b/hostedClusters/cluster-b.yaml",
            ),
            "sites",
        )
        assert set(result) == {
            _mce_wide("telAviv", "prep-mce-tlv-a"),
            _cluster("telAviv", "prep-mce-tlv-b", "cluster-b"),
        }


# ─── _resolve_targets: multiple independent targets ───────────────────────────

class TestResolveTargetsMultiple:
    def test_two_clusters_same_mce(self):
        result = resolve(
            _paths(
                "sites/telAviv/mces/prep-mce-tlv-a/hostedClusters/cluster-a.yaml",
                "sites/telAviv/mces/prep-mce-tlv-a/hostedClusters/cluster-b.yaml",
            ),
            "sites",
        )
        assert set(result) == {
            _cluster("telAviv", "prep-mce-tlv-a", "cluster-a"),
            _cluster("telAviv", "prep-mce-tlv-a", "cluster-b"),
        }

    def test_clusters_in_different_sites(self):
        result = resolve(
            _paths(
                "sites/telAviv/mces/mce-a/hostedClusters/cluster-x.yaml",
                "sites/london/mces/mce-b/hostedClusters/cluster-y.yaml",
            ),
            "sites",
        )
        assert set(result) == {
            _cluster("telAviv", "mce-a", "cluster-x"),
            _cluster("london", "mce-b", "cluster-y"),
        }

    def test_config_values_overrides_everything(self):
        result = resolve(
            _paths(
                "sites/configValues.yaml",
                "sites/telAviv/mces/mce-a/hostedClusters/cluster-x.yaml",
            ),
            "sites",
        )
        assert result is None

    def test_mixed_sites_and_non_sites_files(self):
        result = resolve(
            _paths(
                "app/routers/scopes.py",
                "sites/telAviv/mces/prep-mce-tlv-a/hostedClusters/tel-aviv-prod.yaml",
                "requirements.txt",
            ),
            "sites",
        )
        assert result == [_cluster("telAviv", "prep-mce-tlv-a", "tel-aviv-prod")]


# ─── _resolve_targets: edge cases ────────────────────────────────────────────

class TestResolveTargetsEdgeCases:
    def test_non_yaml_file_in_hosted_clusters_ignored(self):
        result = resolve(
            _paths("sites/telAviv/mces/mce-a/hostedClusters/cluster.txt"),
            "sites",
        )
        assert result == []

    def test_unknown_depth_under_sites_ignored(self):
        result = resolve(
            _paths("sites/telAviv/some/random/deep/file.yaml"),
            "sites",
        )
        assert result == []

    def test_duplicate_same_cluster_in_diff(self):
        # git diff can theoretically list the same file twice (rename + modify)
        result = resolve(
            _paths(
                "sites/telAviv/mces/mce-a/hostedClusters/cluster-x.yaml",
                "sites/telAviv/mces/mce-a/hostedClusters/cluster-x.yaml",
            ),
            "sites",
        )
        assert result == [_cluster("telAviv", "mce-a", "cluster-x")]


# ─── _get_changed_files ───────────────────────────────────────────────────────

class TestGetChangedFiles:
    def test_returns_parsed_paths(self, tmp_path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="sites/telAviv/mces/mce-a/hostedClusters/cluster.yaml\napp/main.py\n",
                returncode=0,
            )
            result = vcc._get_changed_files("origin/main", tmp_path)
        assert result == [
            Path("sites/telAviv/mces/mce-a/hostedClusters/cluster.yaml"),
            Path("app/main.py"),
        ]

    def test_empty_diff_returns_empty_list(self, tmp_path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            result = vcc._get_changed_files("origin/main", tmp_path)
        assert result == []

    def test_git_error_exits_2(self, tmp_path):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                128, "git", stderr="fatal: not a git repo"
            )
            with pytest.raises(SystemExit) as exc_info:
                vcc._get_changed_files("origin/main", tmp_path)
            assert exc_info.value.code == 2

    def test_passes_correct_git_command(self, tmp_path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            vcc._get_changed_files("origin/develop", tmp_path)
        call_args = mock_run.call_args
        assert call_args[0][0] == ["git", "diff", "--name-only", "origin/develop...HEAD"]
        assert call_args[1]["cwd"] == tmp_path


# ─── main() integration ───────────────────────────────────────────────────────

def _run_main(argv: list[str]) -> int:
    """Run main() with given argv, return SystemExit code."""
    with patch("sys.argv", ["validate_changed_clusters.py"] + argv):
        with pytest.raises(SystemExit) as exc_info:
            vcc.main()
    return exc_info.value.code


class TestMain:
    def test_no_changed_files_exits_0(self, tmp_path):
        with patch.object(vcc, "_get_changed_files", return_value=[]):
            code = _run_main(["--repo-root", str(tmp_path)])
        assert code == 0

    def test_no_sites_files_exits_0(self, tmp_path):
        with patch.object(vcc, "_get_changed_files",
                          return_value=[Path("app/main.py")]):
            code = _run_main(["--repo-root", str(tmp_path)])
        assert code == 0

    def test_changed_cluster_calls_run_validation_with_correct_filters(self, tmp_path):
        changed = [Path("sites/telAviv/mces/prep-mce-tlv-a/hostedClusters/tel-aviv-prod.yaml")]
        captured = []

        def fake_run_validation(args):
            captured.append(args)
            return 0

        with patch.object(vcc, "_get_changed_files", return_value=changed):
            with patch.object(vcc, "run_validation" if hasattr(vcc, "run_validation") else "__builtins__",
                              fake_run_validation, create=True):
                # Patch via import inside main
                sys.modules["validate_dhcp_values"] = MagicMock(
                    run_validation=fake_run_validation
                )
                code = _run_main(["--repo-root", str(tmp_path)])

        assert code == 0
        assert len(captured) == 1
        assert captured[0].site == "telAviv"
        assert captured[0].mce == "prep-mce-tlv-a"
        assert captured[0].cluster == "tel-aviv-prod"

    def test_validation_failure_exits_1(self, tmp_path):
        changed = [Path("sites/telAviv/mces/mce-a/hostedClusters/cluster.yaml")]
        sys.modules["validate_dhcp_values"] = MagicMock(run_validation=lambda _: 1)

        with patch.object(vcc, "_get_changed_files", return_value=changed):
            code = _run_main(["--repo-root", str(tmp_path)])

        assert code == 1

    def test_config_values_change_calls_full_scan(self, tmp_path):
        changed = [Path("sites/configValues.yaml")]
        captured = []

        sys.modules["validate_dhcp_values"] = MagicMock(
            run_validation=lambda args: captured.append(args) or 0
        )

        with patch.object(vcc, "_get_changed_files", return_value=changed):
            code = _run_main(["--repo-root", str(tmp_path)])

        assert code == 0
        assert len(captured) == 1
        assert captured[0].site is None
        assert captured[0].mce is None
        assert captured[0].cluster is None

    def test_fail_fast_stops_after_first_failure(self, tmp_path):
        changed = [
            Path("sites/telAviv/mces/mce-a/hostedClusters/cluster-a.yaml"),
            Path("sites/telAviv/mces/mce-a/hostedClusters/cluster-b.yaml"),
        ]
        call_count = 0

        def failing_then_passing(args):
            nonlocal call_count
            call_count += 1
            return 1  # always fail

        sys.modules["validate_dhcp_values"] = MagicMock(run_validation=failing_then_passing)

        with patch.object(vcc, "_get_changed_files", return_value=changed):
            code = _run_main(["--repo-root", str(tmp_path), "--fail-fast"])

        assert code == 1
        assert call_count == 1  # stopped after first failure

    def test_multiple_targets_all_pass(self, tmp_path):
        changed = [
            Path("sites/telAviv/mces/mce-a/hostedClusters/cluster-a.yaml"),
            Path("sites/london/mces/mce-b/hostedClusters/cluster-b.yaml"),
        ]
        call_count = 0

        def passing(_):
            nonlocal call_count
            call_count += 1
            return 0

        sys.modules["validate_dhcp_values"] = MagicMock(run_validation=passing)

        with patch.object(vcc, "_get_changed_files", return_value=changed):
            code = _run_main(["--repo-root", str(tmp_path)])

        assert code == 0
        assert call_count == 2
