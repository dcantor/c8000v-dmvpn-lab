*** Settings ***
Documentation     Customers on VyOS interoperate with the Catalyst hubs: the same underlay, NHRP registration with every hub,
...               IKEv2/IPsec as the hubs negotiate it (tunnel mode, no PFS), iBGP with every hub, and phase 3 shortcuts
...               to the C8000v customers. Seen from the VyOS side (FRR, strongSwan); the Catalyst side is in 03 / 04.
Resource          ../resources/common.resource
Suite Setup       Skip If    not $VYOS_SPOKES    no customer runs on VyOS in this lab
Suite Teardown    Suite Teardown Close Connections
Force Tags        vyos

*** Test Cases ***
Every VyOS customer runs VyOS with its tunnel sourced from its access link
    FOR    ${s}    IN    @{VYOS_SPOKES}
        ${v}=    On VyOS    ${s}    show version
        Should Contain    ${v}    VyOS
        ${t}=    On VyOS    ${s}    show interfaces tunnel tun0
        Should Contain    ${t}    ${ROUTERS}[${s}][tunnel]/32
        Should Contain    ${t}    ${ROUTERS}[${s}][nbma]
    END

Every VyOS customer peers eBGP with the provider and offers it only its access link
    FOR    ${s}    IN    @{VYOS_SPOKES}
        ${n}=    On VyOS    ${s}    sudo vtysh -c 'show ip bgp neighbors ${ROUTERS}[${s}][wan_peer]'
        Should Contain    ${n}    BGP state = Established
        Should Contain    ${n}    remote AS ${PROVIDER_ASN}
        ${adv}=    On VyOS    ${s}    sudo vtysh -c 'show ip bgp neighbors ${ROUTERS}[${s}][wan_peer] advertised-routes'
        Should Contain    ${adv}    ${ROUTERS}[${s}][wan_prefix]
        Should Match Regexp    ${adv}    Total number of prefixes 1\\b
    END

Every VyOS customer is registered with every hub, and every hub holds its registration
    FOR    ${s}    IN    @{VYOS_SPOKES}
        ${c}=    On VyOS    ${s}    sudo vtysh -c 'show ip nhrp cache'
        FOR    ${h}    IN    @{HUBS}
            Should Match Regexp    ${c}    (?m)^\\S+\\s+nhs\\s+${ROUTERS}[${h}][tunnel]\\S*\\s+${ROUTERS}[${h}][nbma]\\s
            ${dm}=    Show    ${h}    show dmvpn | begin Interface
            Should Match Regexp    ${dm}    (?m)^\\s*\\d+\\s+${ROUTERS}[${s}][nbma]\\s+${ROUTERS}[${s}][tunnel]\\s+UP\\s+\\S+\\s+D
        END
    END

IPsec to every hub is up, in tunnel mode, as the hubs negotiate it
    FOR    ${s}    IN    @{VYOS_SPOKES}
        ${sa}=    On VyOS    ${s}    sudo swanctl --list-sas
        FOR    ${h}    IN    @{HUBS}
            Should Match Regexp    ${sa}    (?s)ESTABLISHED.*?${ROUTERS}[${h}][nbma]
        END
        Should Match Regexp    ${sa}    INSTALLED, TUNNEL
        Should Contain    ${sa}    AES_CBC-256/HMAC_SHA2_256_128
        FOR    ${h}    IN    @{HUBS}
            ${ike}=    Show    ${h}    show crypto ikev2 sa remote ${ROUTERS}[${s}][nbma]
            Should Match Regexp    ${ike}    READY
        END
    END

Every VyOS customer peers iBGP with every hub and learns every other LAN through a hub
    FOR    ${s}    IN    @{VYOS_SPOKES}
        ${sum}=    On VyOS    ${s}    sudo vtysh -c 'show bgp ipv4 unicast summary'
        FOR    ${h}    IN    @{HUBS}
            Should Match Regexp    ${sum}    (?m)^${ROUTERS}[${h}][tunnel]\\s+4\\s+${BGP_ASN}(?:\\s+\\d+){5}\\s+\\S+\\s+\\d+\\s+\\d+\\b    msg=${s} is not Established with ${h}
        END
        ${rt}=    On VyOS    ${s}    sudo vtysh -c 'show ip route bgp'
        FOR    ${o}    IN    @{DMVPN}
            Continue For Loop If    '${o}' == '${s}'
            Should Contain    ${rt}    ${ROUTERS}[${o}][lan]
        END
        # the provider's /30s belong to the underlay (eBGP, [20/0] via the provider); none may arrive over the overlay (iBGP, [200/..])
        Should Not Match Regexp    ${rt}    (?m)^B\\S*\\s+${WAN_NET.split('.')[0]}\\.${WAN_NET.split('.')[1]}\\.\\S+\\s+\\[200/    msg=a provider route crossed into the overlay
        Should Match Regexp    ${rt}    (?m)^B\\S*\\s+${WAN_NET.split('.')[0]}\\.${WAN_NET.split('.')[1]}\\.\\S+\\s+\\[20/0\\] via ${ROUTERS}[${s}][wan_peer]
    END

A VyOS customer builds a phase 3 shortcut to a Catalyst customer
    [Documentation]    Traffic from the VyOS customer's host to a C8000v customer's host goes through a hub first; the hub's
    ...                NHRP redirect makes nhrpd resolve the far customer and install a shortcut, after which the path is direct.
    FOR    ${s}    IN    @{VYOS_SPOKES}
        ${o}=    Set Variable    ${C8K_SPOKES}[0]
        ${h}=    Set Variable    ${ROUTERS}[${s}][host_vm]
        On Host    ${h}    ping -c 8 -W 2 ${ROUTERS}[${o}][lan_ip]
        Wait Until Keyword Succeeds    45s    5s    VyOS Has Shortcut To    ${s}    ${o}
        # the NHRP entry can precede the shortcut route by a moment: keep the traffic flowing until the path is direct
        Wait Until Keyword Succeeds    60s    5s    Path Is Direct    ${h}    ${o}
    END

*** Keywords ***
Path Is Direct
    [Arguments]    ${h}    ${o}
    On Host    ${h}    ping -c 5 -i 0.2 -W 1 ${HOST_VMS}[${ROUTERS}[${o}][host_vm]][lan_ip]
    ${tr}=    On Host    ${h}    traceroute -n -q 2 -w 2 -m 6 ${HOST_VMS}[${ROUTERS}[${o}][host_vm]][lan_ip]
    Should Match Regexp    ${tr}[1]    (?m)^\\s*2\\s+${ROUTERS}[${o}][tunnel]\\s    msg=${h} -> ${o} still goes through a hub

VyOS Has Shortcut To
    [Arguments]    ${s}    ${o}
    ${c}=    On VyOS    ${s}    sudo vtysh -c 'show ip nhrp cache'
    Should Match Regexp    ${c}    (?m)^\\S+\\s+dynamic\\s+${ROUTERS}[${o}][tunnel]\\S*\\s+${ROUTERS}[${o}][nbma]\\s
