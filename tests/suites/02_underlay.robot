*** Settings ***
Documentation     The provider and the underlay: every DMVPN router is an eBGP customer of the simulated MPLS network,
...               which carries the sites' WAN addresses — and only those.
Resource          ../resources/common.resource
Suite Teardown    Suite Teardown Close Connections

*** Test Cases ***
Every access link is addressed as modelled and answers
    FOR    ${r}    IN    @{C8K}
        ${br}=    Show    ${r}    show ip interface brief | include ^GigabitEthernet2
        Should Match Regexp    ${br}    GigabitEthernet2\\s+${ROUTERS}[${r}][nbma]\\s+YES\\s+\\S+\\s+up\\s+up
        ${p}=    Show    ${r}    ping ${ROUTERS}[${r}][wan_peer] repeat 3 timeout 1
        Should Match Regexp    ${p}    Success rate is (100|66) percent
    END

The provider has an established eBGP session with every site, arriving on its listen range
    ${sum}=    Provider    show ip bgp summary
    Should Contain    ${sum}    local AS number ${PROVIDER_ASN}
    FOR    ${r}    IN    @{C8K}
        # an established FRR session shows a prefix count in the State/PfxRcd column; anything else is a state name
        Should Match Regexp    ${sum}    (?m)^\\*?${ROUTERS}[${r}][nbma]\\s+4\\s+${BGP_ASN}\\s+.*\\s1\\s+\\d+\\s+\\S+\\s*$
    END
    ${cfg}=    Provider    show configuration commands | match "bgp (listen|peer-group CE)"
    Should Contain    ${cfg}    listen range ${WAN_NET} peer-group 'CE'
    Should Contain    ${cfg}    as-override

Every router peers eBGP with the provider and offers it only its own WAN /30
    FOR    ${r}    IN    @{C8K}
        ${nbr}=    Show    ${r}    show bgp ipv4 unicast neighbors ${ROUTERS}[${r}][wan_peer] | include BGP state|remote AS
        Should Contain    ${nbr}    remote AS ${PROVIDER_ASN}
        Should Contain    ${nbr}    BGP state = Established
        ${adv}=    Show    ${r}    show bgp ipv4 unicast neighbors ${ROUTERS}[${r}][wan_peer] advertised-routes
        Should Contain    ${adv}    ${ROUTERS}[${r}][wan_prefix]
        Should Match Regexp    ${adv}    Total number of prefixes 1\\b
    END

Every router reaches every other router's NBMA address through the provider, and only through it
    FOR    ${r}    IN    @{C8K}
        ${rt}=    Show    ${r}    show ip route bgp
        FOR    ${o}    IN    @{C8K}
            Continue For Loop If    '${r}' == '${o}'
            Should Match Regexp    ${rt}    (?m)^B\\s+${ROUTERS}[${o}][wan_prefix] \\[20/0\\] via ${ROUTERS}[${r}][wan_peer]
            ${p}=    Show    ${r}    ping ${ROUTERS}[${o}][nbma] source GigabitEthernet2 repeat 3 timeout 1
            Should Match Regexp    ${p}    Success rate is (100|66) percent
        END
    END

The provider carries no customer LAN, router-id or overlay route
    ${rt}=    Provider    show ip route bgp
    Should Not Contain    ${rt}    192.168.
    Should Not Contain    ${rt}    172.28.
    Should Not Contain    ${rt}    10.255.5.
