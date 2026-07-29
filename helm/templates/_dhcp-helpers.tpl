{{/*
Derive the default gateway — the subnet's .254 address — for a values file that omits
the key entirely. Resolved here rather than left to the API because Crossplane
byte-compares the GET response to this rendered body: GET always reports the concrete
address the DHCP server holds, so the desired body must carry it too, or the diff never
closes and Crossplane re-PUTs forever.

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

scopeName: {{ $v.scopeName | quote }}
subnetMask: {{ $mask | quote }}
startRange: {{ $v.startRange | quote }}
endRange: {{ $v.endRange | quote }}
leaseDurationDays: {{ $v.leaseDurationDays | int }}
description: {{ $v.description | default "" | quote }}
{{- /* Present-but-empty stays null (no DHCP option 3); only an absent key derives .254. */}}
gateway: {{ if hasKey $v "gateway" }}{{ $v.gateway | default nil | toJson }}{{ else }}{{ include "dhcp.defaultGateway" (dict "network" $v.network "mask" $mask) }}{{ end }}
dnsServers: {{ $dnsServers | toJson }}
dnsDomain: {{ $dnsDomain | quote }}
nextServer: {{ $nextServer | quote }}
bootFile: {{ $bootFile | quote }}
exclusions: {{ $v.exclusions | default (list) | toJson }}
{{- if $useFailover }}
{{- $f := $v.failover }}
failover:
  partnerServer: {{ $f.partnerServer | quote }}
  relationshipName: {{ $f.relationshipName | quote }}
  mode: {{ $f.mode | quote }}
  serverRole: {{ if eq $f.mode "LoadBalance" }}"Active"{{ else }}{{ $f.serverRole | quote }}{{ end }}
  reservePercent: {{ if eq $f.mode "LoadBalance" }}0{{ else }}{{ $f.reservePercent | default 0 | int }}{{ end }}
  loadBalancePercent: {{ if eq $f.mode "HotStandby" }}0{{ else }}{{ $f.loadBalancePercent | int }}{{ end }}
  maxClientLeadTimeMinutes: {{ $f.maxClientLeadTimeMinutes | int }}
{{- else }}
failover: null
{{- end }}
{{- end }}
