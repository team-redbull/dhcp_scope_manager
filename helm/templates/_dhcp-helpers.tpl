{{/*
Derive the default gateway — the subnet's .254 address — for a values file that omits
the key entirely. Resolved here rather than left to the API because Crossplane
checks the GET response against this rendered body: GET always reports the concrete
address the DHCP server holds, so the desired body must carry it too, or the check never
passes and Crossplane re-PUTs forever.

The .254 convention only holds for a /24, so any other mask without an explicit gateway
fails the render rather than guessing. Mirrors
DhcpScopePayload.resolve_default_gateway in app/models/scope.py.

Takes a dict with "network" and "mask". Emits a quoted IPv4 address.
*/}}
{{- define "dhcp.defaultGateway" -}}
{{- $mask := .mask -}}
{{- if ne $mask "255.255.255.0" -}}
{{- fail (printf "dhcp_values.gateway is required when subnetMask is %s: the default gateway is only derivable for 255.255.255.0. Set dhcp_values.gateway explicitly, or set it to \"\" for no gateway." $mask) -}}
{{- end -}}
{{- $octets := splitList "." (required "dhcp_values.network is required" .network) -}}
{{- if ne (len $octets) 4 -}}
{{- fail (printf "dhcp_values.network %q is not a valid IPv4 address" .network) -}}
{{- end -}}
{{- printf "%s.%s.%s.254" (index $octets 0) (index $octets 1) (index $octets 2) | quote -}}
{{- end -}}

{{/*
The request body, as a JSON document.

Emitted as JSON text rather than as a YAML mapping because provider-http types
spec.forProvider.payload.body as a *string* — a nested mapping there is rejected
outright by the API server ("must be of type string"). The mappings then reach
into it with jq (.payload.body), which parses this text back into an object.

Written out field by field instead of piped through `toJson` so the field order
survives: Go marshals a map with its keys sorted, and this order is the canonical
one documented in CLAUDE.md section 5 and asserted in tests/test_helm.py.
*/}}
{{- define "dhcp.payload" -}}
{{- $v := .Values.dhcp_values | default dict -}}

{{- $dns := $v.dns | default dict -}}
{{- $dnsServers := $dns.servers | default (list) -}}
{{- $dnsDomain := $dns.domain | default "" -}}

{{- /* PXE options 66/67. Optional, but both-or-nothing — the API and the CI validator
       both reject half a pair, so the render is a straight pass-through. Absent keys
       become "", which is the concrete "no PXE options" state GET reports back. */}}
{{- $pxe := $v.pxe | default dict -}}
{{- $nextServer := $pxe.server | default "" -}}
{{- $bootFile := $pxe.bootfile | default "" -}}

{{- $mask := $v.subnetMask | default "255.255.255.0" -}}

{{- $useFailover := and (hasKey $v "failover") $v.failover -}}

{
  "scopeName": {{ $v.scopeName | toJson }},
  "subnetMask": {{ $mask | toJson }},
  "startRange": {{ $v.startRange | toJson }},
  "endRange": {{ $v.endRange | toJson }},
  "leaseDurationDays": {{ $v.leaseDurationDays | int }},
  "description": {{ $v.description | default "" | toJson }},
  {{- /* Present-but-empty stays null (no DHCP option 3); only an absent key derives .254. */}}
  "gateway": {{ if hasKey $v "gateway" }}{{ $v.gateway | default nil | toJson }}{{ else }}{{ include "dhcp.defaultGateway" (dict "network" $v.network "mask" $mask) }}{{ end }},
  "dnsServers": {{ $dnsServers | toJson }},
  "dnsDomain": {{ $dnsDomain | toJson }},
  "nextServer": {{ $nextServer | toJson }},
  "bootFile": {{ $bootFile | toJson }},
  "exclusions": {{ $v.exclusions | default (list) | toJson }},
{{- if $useFailover }}
{{- $f := $v.failover }}
  "failover": {
    "partnerServer": {{ $f.partnerServer | toJson }},
    "relationshipName": {{ $f.relationshipName | toJson }},
    "mode": {{ $f.mode | toJson }},
    "serverRole": {{ if eq $f.mode "LoadBalance" }}"Active"{{ else }}{{ $f.serverRole | toJson }}{{ end }},
    "reservePercent": {{ if eq $f.mode "LoadBalance" }}0{{ else }}{{ $f.reservePercent | default 0 | int }}{{ end }},
    "loadBalancePercent": {{ if eq $f.mode "HotStandby" }}0{{ else }}{{ $f.loadBalancePercent | int }}{{ end }},
    "maxClientLeadTimeMinutes": {{ $f.maxClientLeadTimeMinutes | int }}
  }
{{- else }}
  "failover": null
{{- end }}
}
{{- end }}
