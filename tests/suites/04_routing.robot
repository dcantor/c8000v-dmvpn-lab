*** Settings ***
Documentation     iBGP over the overlay: three hubs reflecting, customers peering with every hub, every LAN learned
...               everywhere with the originating router as next hop, the planes kept apart, and the hosts talking.
Resource          ../resources/common.resource
Suite Teardown    Suite Teardown Close Connections

*** Test Cases ***
Every hub accepts every customer on its listen range as a route-reflector client
    FOR    ${h}    IN    @{HUBS}
        ${sum}=    Show    ${h}    show bgp ipv4 unicast summary
        Should Contain    ${sum}    local AS number ${BGP_ASN}
        Should Match Regexp    ${sum}    (?m)^\\* ${ROUTERS}[${SPOKES}[0]][tunnel]|dynamically created
        FOR    ${s}    IN    @{SPOKES}
            ${nbr}=    Show    ${h}    show bgp ipv4 unicast neighbors ${ROUTERS}[${s}][tunnel] | include BGP state|Route-Reflector|peer-group|dynamic
            Should Contain    ${nbr}    BGP state = Established
            Should Contain    ${nbr}    Route-Reflector Client
            Should Contain    ${nbr}    CUSTOMERS
        END
    END

The hubs peer with each other in the overlay
    FOR    ${h}    IN    @{HUBS}
        FOR    ${o}    IN    @{HUBS}
            Continue For Loop If    '${h}' == '${o}'
            ${nbr}=    Show    ${h}    show bgp ipv4 unicast neighbors ${ROUTERS}[${o}][tunnel] | include BGP state
            Should Contain    ${nbr}    BGP state = Established
        END
    END

Every customer peers with all three hubs, and only with them in the overlay
    FOR    ${s}    IN    @{C8K_SPOKES}
        ${sum}=    Show    ${s}    show bgp ipv4 unicast summary | begin Neighbor
        FOR    ${h}    IN    @{HUBS}
            Should Match Regexp    ${sum}    (?m)^${ROUTERS}[${h}][tunnel]\\s+4\\s+${BGP_ASN}\\s+.*\\s\\d+\\s*$
        END
        ${count}=    Get Lines Matching Regexp    ${sum}    ^\\*?172\\.28\\.    partial_match=True
        ${n}=    Get Line Count    ${count}
        Should Be Equal As Integers    ${n}    3    msg=${s} must peer with the three hubs only
    END

Every router has every other LAN and router-id: a hub from the originating router, a customer through a hub
    [Documentation]    The hubs reflect to their customers with themselves as next hop (next-hop-self all), so a customer
    ...                reaches another site through a hub until phase 3 overrides the next hop with the shortcut (`%`);
    ...                a customer that prefers a hub goes through that one. A hub has every prefix from its originator.
    FOR    ${r}    IN    @{C8K}
        ${rt}=    Show    ${r}    show ip route bgp
        FOR    ${o}    IN    @{DMVPN}
            Continue For Loop If    '${r}' == '${o}'
            IF    '${ROUTERS}[${r}][role]' == 'hub'
                ${via}=    Evaluate    __import__('re').escape($ROUTERS[$o]['tunnel'])
            ELSE IF    $r in $PREFER and $o in $SPOKES
                ${via}=    Evaluate    __import__('re').escape($ROUTERS[$PREFER[$r]]['tunnel'])
            ELSE
                ${via}=    Set Variable    ${HUB_TUNNELS_RE}
            END
            Should Match Regexp    ${rt}    (?m)^B\\s+(%\\s+)?${ROUTERS}[${o}][lan] \\[200/0\\] via ${via}\\b
            Should Match Regexp    ${rt}    (?m)^B\\s+(%\\s+)?${ROUTERS}[${o}][router_id](/32)? \\[200/0\\] via ${via}\\b
        END
    END

The hubs send themselves as next hop to their customers, over both clouds
    FOR    ${h}    IN    @{HUBS}
        ${b}=    Show    ${h}    show running-config | section router bgp
        Should Contain    ${b}    neighbor CUSTOMERS next-hop-self all
        IF    $h in $DUAL_HUBS
            Should Contain    ${b}    bgp listen range 172.29.0.0/24 peer-group CUSTOMERS2
            Should Contain    ${b}    neighbor CUSTOMERS2 next-hop-self all
        END
    END

A dual-homed customer has every other site over both clouds, the second one ranked below the first
    [Documentation]    Cloud 2's routes carry local-preference 50: they are the backup, used when the first provider fails.
    Skip If    not $DUAL_SPOKES    no customer is dual-homed
    FOR    ${c}    IN    @{DUAL_SPOKES}
        ${other}=    Evaluate    [s for s in $SPOKES if s != $c][0]
        IF    '${PLATFORM}[${c}]' == 'vyos'
            ${out}=    On VyOS    ${c}    sudo vtysh -c 'show ip bgp ${ROUTERS}[${other}][lan]'
        ELSE
            ${out}=    Show    ${c}    show ip bgp ${ROUTERS}[${other}][lan]
        END
        Should Contain    ${out}    localpref 50
        ${best}=    Evaluate    [b for b in __import__('re').split(r'\\n(?=\\s{2}(?:Local|Refresh))', $out) if ', best' in b]
        Should Not Be Empty    ${best}
        Should Not Contain    ${best}[0]    localpref 50    msg=${c} prefers the backup cloud for ${ROUTERS}[${other}][lan]
    END

No provider route enters the overlay and no overlay route resolves through it
    FOR    ${r}    IN    @{C8K}
        ${ibgp}=    Show    ${r}    show ip route bgp | include \\[200/
        Should Not Contain    ${ibgp}    100.70.
        FOR    ${o}    IN    @{DMVPN}
            Continue For Loop If    '${r}' == '${o}'
            ${one}=    Show    ${r}    show ip route ${ROUTERS}[${o}][lan_ip]
            Should Not Contain    ${one}    via ${ROUTERS}[${r}][wan_peer]
        END
    END

Every site LAN reaches every other site LAN through the overlay
    FOR    ${r}    IN    @{C8K}
        FOR    ${o}    IN    @{DMVPN}
            Continue For Loop If    '${r}' == '${o}'
            ${src}=    Set Variable If    '${ROUTERS}[${r}][role]' == 'hub'    Loopback10    ${ROUTERS}[${r}][lan_ip]
            ${p}=    Show    ${r}    ping ${ROUTERS}[${o}][lan_ip] source ${src} repeat 5
            Should Match Regexp    ${p}    Success rate is (100|80|60) percent
        END
    END

Every host pings every other host
    FOR    ${h}    IN    @{HOSTS}
        FOR    ${o}    IN    @{HOSTS}
            Continue For Loop If    '${h}' == '${o}'
            ${res}=    On Host    ${h}    ping -c 3 -W 2 ${HOST_VMS}[${o}][lan_ip]
            Should Be Equal As Integers    ${res}[0]    0    msg=${h} cannot reach ${o}
        END
    END

A host's traffic to another customer leaves over the direct tunnel
    [Documentation]    After the shortcut has formed, the path from one customer's host is its router, then the far customer's
    ...                tunnel address — not a hub.
    Skip If    not $HOST_PAIR    fewer than two customer hosts
    On Host    ${HOST_PAIR}[0]    ping -c 5 -W 2 ${HOST_VMS}[${HOST_PAIR}[1]][lan_ip]
    Wait Until Keyword Succeeds    45s    5s    Host Path Is Direct    ${HOST_PAIR}[0]    ${HOST_PAIR}[1]

*** Keywords ***
Host Path Is Direct
    [Arguments]    ${h}    ${o}
    ${res}=    On Host    ${h}    traceroute -n -q 1 -w 2 ${HOST_VMS}[${o}][lan_ip]
    ${far}=    Set Variable    ${HOST_VMS}[${o}][router]
    Should Match Regexp    ${res}[1]    (?m)^\\s*2\\s+${ROUTERS}[${far}][tunnel]\\s

A customer that prefers a hub takes the other customers' routes through it, and only its own preference applies
    [Documentation]    PREFER_HUB in lab.conf: the preferred hub's copy of every other customer's LAN is best with
    ...                local-preference 200 and the hub as next hop; a customer without a preference has no such policy.
    Skip If    not $PREFERRING    no customer prefers a hub right now
    FOR    ${c}    IN    @{PREFERRING}
        ${other}=    Evaluate    [s for s in $SPOKES if s != $c][0]
        ${lan}=    Set Variable    ${ROUTERS}[${other}][lan]
        ${hub}=    Set Variable    ${ROUTERS}[${PREFER}[${c}]][tunnel]
        IF    '${PLATFORM}[${c}]' == 'vyos'
            ${out}=    On VyOS    ${c}    sudo vtysh -c 'show ip bgp ${lan}'
        ELSE
            ${out}=    Show    ${c}    show ip bgp ${lan}
        END
        ${best}=    Evaluate    [b for b in __import__('re').split(r'\\n(?=\\s{2}Local)', $out) if 'best' in b.split('\\n')[2] or ', best' in b]
        Should Not Be Empty    ${best}    msg=${c} has no best path to ${lan}
        Should Contain    ${best}[0]    ${hub} from ${hub}    msg=${c}'s best path to ${lan} is not through ${PREFER}[${c}]
        Should Contain    ${best}[0]    localpref 200
    END
    FOR    ${c}    IN    @{C8K_SPOKES}
        Continue For Loop If    $c in $PREFER
        ${rm}=    Show    ${c}    show running-config | include ^route-map OVERLAY-HUB
        Should Be Empty    ${rm.strip()}    msg=${c} prefers no hub but has a per-hub policy
    END
