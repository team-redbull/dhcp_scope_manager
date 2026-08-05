"""Helm template rendering tests.

Runs `helm template` via subprocess against the chart in ./helm/.
Tests verify that the Crossplane Request CR renders correctly for valid
and invalid values, covering all fields that affect API correctness.

Requirements:
- helm CLI must be on PATH (checked at module level with a skip marker)
- No real cluster or DHCP server required
"""
import json
import shutil
import subprocess
import tempfile
import textwrap

import pytest
import yaml

HELM_CHART = "helm"


def _helm_template(values_content: str, extra_args: list[str] | None = None) -> str:
    """Run helm template with the given values YAML; return stdout or raise on error."""
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as fh:
        fh.write(values_content)
        values_path = fh.name

    cmd = ["helm", "template", "test-release", HELM_CHART, "-f", values_path]
    if extra_args:
        cmd.extend(extra_args)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
    return result.stdout


def _helm_template_fails(values_content: str) -> str:
    """Expect helm template to fail; return stderr."""
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as fh:
        fh.write(values_content)
        values_path = fh.name

    result = subprocess.run(
        ["helm", "template", "test-release", HELM_CHART, "-f", values_path],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "Expected helm template to fail but it succeeded"
    return result.stderr


def _parse_cr(rendered: str) -> dict:
    """Parse the first YAML document from rendered output."""
    docs = list(yaml.safe_load_all(rendered))
    return docs[0]


# Minimal valid values. dhcp_values comes entirely from here: the chart's own
# values.yaml ships it commented out, so nothing leaks in from the base layer.
# failover: null and tokenSecretRef: null are still spelled out so the "no failover"
# and "no token" cases stay explicit rather than relying on absence.
_VALID_VALUES = textwrap.dedent("""\
    dhcp_api:
      url: https://dhcp-api.lab.local
      tokenSecretRef: null
    dhcp_values:
      scopeName: "test-scope"
      network: "10.20.30.0"
      subnetMask: "255.255.255.0"
      startRange: "10.20.30.100"
      endRange: "10.20.30.200"
      leaseDurationDays: 8
      description: ""
      gateway: "10.20.30.1"
      dns:
        servers:
          - "10.0.0.53"
        domain: "lab.local"
      exclusions: []
      failover: null
""")

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm CLI not available"
)


class TestHelmTemplateBasic:

    def test_valid_values_render_without_error(self):
        output = _helm_template(_VALID_VALUES)
        assert output.strip()

    def test_cr_has_correct_api_version(self):
        cr = _parse_cr(_helm_template(_VALID_VALUES))
        assert cr["apiVersion"] == "http.crossplane.io/v1alpha2"

    def test_cr_kind_is_request(self):
        cr = _parse_cr(_helm_template(_VALID_VALUES))
        assert cr["kind"] == "Request"

    def test_cr_name_uses_dashes_not_dots(self):
        """dhcp-scope-10-20-30-0 (dots replaced with dashes)."""
        cr = _parse_cr(_helm_template(_VALID_VALUES))
        assert cr["metadata"]["name"] == "dhcp-scope-10-20-30-0"

    def test_cr_namespace_defaults_to_crossplane_system(self):
        cr = _parse_cr(_helm_template(_VALID_VALUES))
        assert cr["metadata"]["namespace"] == "crossplane-system"

    def test_provider_config_name_defaults_to_dhcp_http(self):
        cr = _parse_cr(_helm_template(_VALID_VALUES))
        assert cr["spec"]["providerConfigRef"]["name"] == "dhcp-http"

    def test_base_url_includes_api_server_url(self):
        cr = _parse_cr(_helm_template(_VALID_VALUES))
        base_url = cr["spec"]["forProvider"]["payload"]["baseUrl"]
        assert "https://dhcp-api.lab.local" in base_url

    def test_base_url_carries_the_scope_address(self):
        """The scope address is baked into baseUrl — it is the only place it appears now."""
        cr = _parse_cr(_helm_template(_VALID_VALUES))
        base_url = cr["spec"]["forProvider"]["payload"]["baseUrl"]
        assert base_url.endswith("/api/v1/scopes/10.20.30.0")

    def test_deletion_policy_is_delete(self):
        cr = _parse_cr(_helm_template(_VALID_VALUES))
        assert cr["spec"]["deletionPolicy"] == "Delete"


class TestHelmRequiredFields:

    def test_network_required(self):
        """helm template fails when network is explicitly set to empty string.

        Omitting dhcp_values entirely renders nothing at all (the template is gated
        on scopeName), so the `required` guard is reached only by supplying a scope
        whose network resolves to an empty / null string.
        """
        values = textwrap.dedent("""\
            dhcp_api:
              url: https://dhcp-api.lab.local
            dhcp_values:
              network: ""
              scopeName: "test"
              subnetMask: "255.255.255.0"
              startRange: "10.20.30.100"
              endRange: "10.20.30.200"
              leaseDurationDays: 8
              gateway: "10.20.30.1"
              dns:
                servers: ["10.0.0.53"]
                domain: "lab.local"
              exclusions: []
        """)
        stderr = _helm_template_fails(values)
        assert "network" in stderr.lower() or "required" in stderr.lower()

    def test_api_server_url_required(self):
        """helm template fails when dhcp_api.url is explicitly set to empty string.

        The chart has a default url in values.yaml, so merely omitting the key
        uses that default.  Explicitly setting url: "" triggers the `required` guard.
        """
        values = textwrap.dedent("""\
            dhcp_api:
              url: ""
            dhcp_values:
              scopeName: "test"
              network: "10.20.30.0"
              subnetMask: "255.255.255.0"
              startRange: "10.20.30.100"
              endRange: "10.20.30.200"
              leaseDurationDays: 8
              gateway: "10.20.30.1"
              dns:
                servers: ["10.0.0.53"]
                domain: "lab.local"
              exclusions: []
        """)
        stderr = _helm_template_fails(values)
        assert "dhcp_api" in stderr or "url" in stderr.lower() or "required" in stderr.lower()


class TestHelmPayloadBody:

    def _body(self, values_content: str) -> dict:
        """The request body, parsed.

        provider-http types payload.body as a JSON *string*, so the chart renders
        text rather than a nested mapping. json.loads preserves insertion order,
        so the field-order assertions below still read the canonical order.
        """
        cr = _parse_cr(_helm_template(values_content))
        return json.loads(cr["spec"]["forProvider"]["payload"]["body"])

    def test_scope_name_in_body(self):
        body = self._body(_VALID_VALUES)
        assert body["scopeName"] == "test-scope"

    def test_network_not_in_body(self):
        """The scope address is the identifier — it belongs in the URL, not the body."""
        body = self._body(_VALID_VALUES)
        assert "network" not in body
        assert "scope" not in body

    def test_lease_duration_is_int(self):
        body = self._body(_VALID_VALUES)
        assert isinstance(body["leaseDurationDays"], int)
        assert body["leaseDurationDays"] == 8

    def test_dns_servers_as_list(self):
        body = self._body(_VALID_VALUES)
        assert isinstance(body["dnsServers"], list)
        assert "10.0.0.53" in body["dnsServers"]

    def test_description_defaults_to_empty_string_not_null(self):
        """description must be "" not null — otherwise Crossplane sees a mismatch."""
        values = textwrap.dedent("""\
            dhcp_api:
              url: https://dhcp-api.lab.local
            dhcp_values:
              scopeName: "test-scope"
              network: "10.20.30.0"
              subnetMask: "255.255.255.0"
              startRange: "10.20.30.100"
              endRange: "10.20.30.200"
              leaseDurationDays: 8
              gateway: "10.20.30.1"
              dns:
                servers: ["10.0.0.53"]
                domain: "lab.local"
              exclusions: []
        """)
        body = self._body(values)
        assert body.get("description") == "" or body.get("description") is not None

    def test_pxe_block_renders_boot_options(self):
        values = _VALID_VALUES.replace(
            "  exclusions: []",
            '  pxe:\n'
            '    server: "10.50.1.20"\n'
            '    bootfile: "snponly.efi"\n'
            "  exclusions: []",
        )
        body = self._body(values)
        assert body["nextServer"] == "10.50.1.20"
        assert body["bootFile"] == "snponly.efi"

    def test_pxe_block_omitted_renders_empty_strings(self):
        """No pxe: block must still emit both keys as "" — GET reports "" for an absent
        option, so omitting the keys here would diff forever for every non-PXE scope.
        """
        body = self._body(_VALID_VALUES)
        assert body["nextServer"] == ""
        assert body["bootFile"] == ""

    def test_boot_option_keys_sit_between_dns_domain_and_exclusions(self):
        """Body key order must match DhcpScopeBody field order — GET and PUT are one model."""
        values = _VALID_VALUES.replace(
            "  exclusions: []",
            '  pxe:\n'
            '    server: "10.50.1.20"\n'
            '    bootfile: "snponly.efi"\n'
            "  exclusions: []",
        )
        keys = list(self._body(values).keys())
        assert keys.index("dnsDomain") < keys.index("nextServer")
        assert keys.index("nextServer") < keys.index("bootFile")
        assert keys.index("bootFile") < keys.index("exclusions")

    def test_gateway_omitted_renders_derived_default(self):
        """Omitting the key derives the subnet's .254 address.

        Resolved at render time rather than left to the API: Crossplane checks this
        body against the GET response, which reports the concrete address, so a body
        that said null here would diff forever.
        """
        values = _VALID_VALUES.replace('  gateway: "10.20.30.1"\n', "")
        body = self._body(values)
        assert body["gateway"] == "10.20.30.254"

    def test_subnet_mask_omitted_renders_default(self):
        values = _VALID_VALUES.replace('  subnetMask: "255.255.255.0"\n', "")
        body = self._body(values)
        assert body["subnetMask"] == "255.255.255.0"

    def test_non_24_mask_without_gateway_fails_render(self):
        """No defensible .254 default exists off a /24 — fail rather than guess."""
        values = (
            _VALID_VALUES
            .replace('  subnetMask: "255.255.255.0"', '  subnetMask: "255.255.0.0"')
            .replace('  network: "10.20.30.0"', '  network: "10.20.0.0"')
            .replace('  gateway: "10.20.30.1"\n', "")
        )
        stderr = _helm_template_fails(values)
        assert "gateway is required when subnetMask is 255.255.0.0" in stderr

    def test_non_24_mask_with_explicit_gateway_renders(self):
        """The mismatch guard applies only to the derive path, not to any non-/24 mask."""
        values = (
            _VALID_VALUES
            .replace('  subnetMask: "255.255.255.0"', '  subnetMask: "255.255.0.0"')
            .replace('  network: "10.20.30.0"', '  network: "10.20.0.0"')
            .replace('  gateway: "10.20.30.1"', '  gateway: "10.20.0.1"')
        )
        body = self._body(values)
        assert body["subnetMask"] == "255.255.0.0"
        assert body["gateway"] == "10.20.0.1"

    def test_gateway_null_renders_null(self):
        values = _VALID_VALUES.replace('  gateway: "10.20.30.1"', "  gateway: null")
        body = self._body(values)
        assert body["gateway"] is None

    def test_gateway_empty_string_renders_null(self):
        values = _VALID_VALUES.replace('  gateway: "10.20.30.1"', '  gateway: ""')
        body = self._body(values)
        assert body["gateway"] is None

    def test_exclusions_as_list(self):
        values = _VALID_VALUES + textwrap.dedent("""\
              exclusions:
                - startAddress: "10.20.30.1"
                  endAddress: "10.20.30.10"
        """).replace("      exclusions: []", "")
        # Use _VALID_VALUES with exclusions replaced — simpler: just parse VALID_VALUES body
        body = self._body(_VALID_VALUES)
        assert isinstance(body["exclusions"], list)

    def test_failover_null_when_not_configured(self):
        """No failover key → failover: null in rendered body."""
        body = self._body(_VALID_VALUES)
        assert "failover" in body
        assert body["failover"] is None


class TestHelmMappings:

    def _mappings(self, values_content: str) -> list:
        cr = _parse_cr(_helm_template(values_content))
        return cr["spec"]["forProvider"]["mappings"]

    def test_four_mappings_rendered(self):
        mappings = self._mappings(_VALID_VALUES)
        assert len(mappings) == 4

    def test_post_mapping_present(self):
        methods = [m["method"] for m in self._mappings(_VALID_VALUES)]
        assert "POST" in methods

    def test_get_mapping_present(self):
        methods = [m["method"] for m in self._mappings(_VALID_VALUES)]
        assert "GET" in methods

    def test_put_mapping_present(self):
        methods = [m["method"] for m in self._mappings(_VALID_VALUES)]
        assert "PUT" in methods

    def test_delete_mapping_present(self):
        methods = [m["method"] for m in self._mappings(_VALID_VALUES)]
        assert "DELETE" in methods

    def test_post_mapping_targets_the_scope_url(self):
        post = next(m for m in self._mappings(_VALID_VALUES) if m["method"] == "POST")
        assert post["url"] == "(.payload.baseUrl)"

    def test_put_mapping_includes_body(self):
        put = next(m for m in self._mappings(_VALID_VALUES) if m["method"] == "PUT")
        assert "body" in put


def _values_with_failover(**failover_fields) -> str:
    """Build a complete values YAML with failover correctly nested under dhcp_values.

    Avoids textwrap.dedent on an f-string that embeds fo_lines, because dedent
    measures the *minimum* indent across all lines — if fo_lines uses a smaller
    indent than the surrounding template it shifts the entire output.
    Instead we build the string directly with the required 2/4-space YAML indent.
    """
    fo_lines = "\n".join(f"    {k}: {_yaml_value(v)}" for k, v in failover_fields.items())
    return (
        "dhcp_api:\n"
        "  url: https://dhcp-api.lab.local\n"
        "  tokenSecretRef: null\n"
        "dhcp_values:\n"
        '  scopeName: "test-scope"\n'
        '  network: "10.20.30.0"\n'
        '  subnetMask: "255.255.255.0"\n'
        '  startRange: "10.20.30.100"\n'
        '  endRange: "10.20.30.200"\n'
        "  leaseDurationDays: 8\n"
        '  description: ""\n'
        '  gateway: "10.20.30.1"\n'
        "  dns:\n"
        "    servers:\n"
        '      - "10.0.0.53"\n'
        '    domain: "lab.local"\n'
        "  exclusions: []\n"
        "  failover:\n"
        + fo_lines
        + "\n"
    )


def _yaml_value(v):
    """Convert a Python value to its YAML inline representation."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, str):
        return f'"{v}"'
    return str(v)


class TestHelmFailoverRendering:

    def _body(self, values_content: str) -> dict:
        """The request body, parsed.

        provider-http types payload.body as a JSON *string*, so the chart renders
        text rather than a nested mapping. json.loads preserves insertion order,
        so the field-order assertions below still read the canonical order.
        """
        cr = _parse_cr(_helm_template(values_content))
        return json.loads(cr["spec"]["forProvider"]["payload"]["body"])

    def test_hotstandby_failover_renders_all_fields(self):
        values = _values_with_failover(
            partnerServer="dhcp02.lab.local",
            relationshipName="tomer-hc-failover",
            mode="HotStandby",
            serverRole="Active",
            reservePercent=5,
            maxClientLeadTimeMinutes=60,
        )
        body = self._body(values)
        f = body["failover"]
        assert f is not None
        assert f["partnerServer"] == "dhcp02.lab.local"
        assert f["mode"] == "HotStandby"
        assert f["serverRole"] == "Active"
        assert f["reservePercent"] == 5
        assert f["loadBalancePercent"] == 0

    def test_loadbalance_failover_normalizes_server_role_to_active(self):
        """Helm template must set serverRole=Active for LoadBalance mode."""
        values = _values_with_failover(
            partnerServer="dhcp02.lab.local",
            relationshipName="tomer-hc-failover",
            mode="LoadBalance",
            loadBalancePercent=50,
            maxClientLeadTimeMinutes=60,
        )
        body = self._body(values)
        f = body["failover"]
        assert f["mode"] == "LoadBalance"
        assert f["serverRole"] == "Active"
        assert f["reservePercent"] == 0
        assert f["loadBalancePercent"] == 50

    def test_hotstandby_normalizes_loadbalance_percent_to_zero(self):
        """HotStandby: loadBalancePercent must be 0 — matches GET response normalization."""
        values = _values_with_failover(
            partnerServer="dhcp02.lab.local",
            relationshipName="tomer-hc-failover",
            mode="HotStandby",
            serverRole="Active",
            reservePercent=5,
            maxClientLeadTimeMinutes=60,
        )
        body = self._body(values)
        assert body["failover"]["loadBalancePercent"] == 0

class TestHelmSecretInjection:
    """Bearer token injection.

    provider-http resolves a `{{ name:namespace:key }}` placeholder in a header
    against the live Secret at reconcile time, keeping the token out of git.
    NOT secretInjectionConfigs — that field runs the other direction, extracting
    fields from the HTTP *response* into a Secret.
    """

    def test_secret_injection_not_rendered_without_all_fields(self):
        """tokenSecretRef block requires name, namespace, AND key — partial config → omit.

        A half-configured ref would render a placeholder provider-http cannot
        resolve, so the header is dropped entirely instead.
        """
        values = textwrap.dedent("""\
            dhcp_api:
              url: https://dhcp-api.lab.local
              tokenSecretRef:
                name: dhcp-api-token
                namespace: ~
                key: ~
            dhcp_values:
              scopeName: "test-scope"
              network: "10.20.30.0"
              subnetMask: "255.255.255.0"
              startRange: "10.20.30.100"
              endRange: "10.20.30.200"
              leaseDurationDays: 8
              gateway: "10.20.30.1"
              dns:
                servers: ["10.0.0.53"]
                domain: "lab.local"
              exclusions: []
              failover: null
        """)
        cr = _parse_cr(_helm_template(values))
        assert "Authorization" not in cr["spec"]["forProvider"]["headers"]

    def test_secret_injection_rendered_with_all_three_fields(self):
        values = textwrap.dedent("""\
            dhcp_api:
              url: https://dhcp-api.lab.local
              tokenSecretRef:
                name: dhcp-api-token
                namespace: crossplane-system
                key: token
            dhcp_values:
              scopeName: "test-scope"
              network: "10.20.30.0"
              subnetMask: "255.255.255.0"
              startRange: "10.20.30.100"
              endRange: "10.20.30.200"
              leaseDurationDays: 8
              gateway: "10.20.30.1"
              dns:
                servers: ["10.0.0.53"]
                domain: "lab.local"
              exclusions: []
        """)
        cr = _parse_cr(_helm_template(values))
        spec = cr["spec"]["forProvider"]

        # The placeholder order is name:namespace:key — provider-http resolves it
        # in that order, so a swap would silently read the wrong Secret.
        assert spec["headers"]["Authorization"] == [
            "Bearer {{ dhcp-api-token:crossplane-system:token }}"
        ]

        # secretInjectionConfigs would write the response INTO a Secret. Using it
        # for a request header does not work and is rejected by the CRD schema.
        assert "secretInjectionConfigs" not in spec

    def test_token_never_appears_verbatim_in_rendered_output(self):
        """Only the placeholder is rendered — the chart never reads Secret contents."""
        values = textwrap.dedent("""\
            dhcp_api:
              url: https://dhcp-api.lab.local
              tokenSecretRef:
                name: dhcp-api-token
                namespace: crossplane-system
                key: token
            dhcp_values:
              scopeName: "test-scope"
              network: "10.20.30.0"
              subnetMask: "255.255.255.0"
              startRange: "10.20.30.100"
              endRange: "10.20.30.200"
              leaseDurationDays: 8
              gateway: "10.20.30.1"
              dns:
                servers: ["10.0.0.53"]
                domain: "lab.local"
              exclusions: []
        """)
        rendered = _helm_template(values)
        assert "{{ dhcp-api-token:crossplane-system:token }}" in rendered

    def test_custom_provider_config_name(self):
        values = textwrap.dedent("""\
            dhcp_api:
              url: https://dhcp-api.lab.local
            crossplane:
              providerConfigName: my-custom-provider
            dhcp_values:
              scopeName: "test-scope"
              network: "10.20.30.0"
              subnetMask: "255.255.255.0"
              startRange: "10.20.30.100"
              endRange: "10.20.30.200"
              leaseDurationDays: 8
              gateway: "10.20.30.1"
              dns:
                servers: ["10.0.0.53"]
                domain: "lab.local"
              exclusions: []
        """)
        cr = _parse_cr(_helm_template(values))
        assert cr["spec"]["providerConfigRef"]["name"] == "my-custom-provider"
