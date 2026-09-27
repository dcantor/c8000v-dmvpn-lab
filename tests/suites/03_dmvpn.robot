*** Settings ***
Documentation     One DMVPN phase 3 cloud: mGRE Tunnel0 on every router, three hubs, every customer registered with all
...               three, IKEv2/IPsec protection, and dynamic customer-to-customer tunnels.
Resource          ../resources/common.resource
Suite Teardown    Suite Teardown Close Connections

*** Test Cases ***
Tunnel0 is an mGRE interface sourced from the WAN and protected by the IPsec profile
    FOR    ${r}    IN    @{C8K}
        ${cfg}=    Show    ${r}    show running-config interface Tunnel0
        Should Contain    ${cfg}    ip address ${ROUTERS}[${r}][tunnel] 255.255.255.0
        Should Contain    ${cfg}    tunnel mode gre multipoint
        Should Contain    ${cfg}    tunnel key ${TUNNEL_KEY}
        Should Contain    ${cfg}    tunnel source GigabitEthernet2
        Should Contain    ${cfg}    ip nhrp network-id ${NHRP_NETWORK_ID}
        Should Contain    ${cfg}    tunnel protection ipsec profile ${IPSEC_PROFILE}
        ${brief}=    Show    ${r}    show ip interface brief | include ^Tunnel0
        Should Match Regexp    ${brief}    Tunnel0\\s+${ROUTERS}[${r}][tunnel]\\s+YES\\s+\\S+\\s+up\\s+up
    END

Every hub is an NHRP server holding a registration for every customer
    FOR    ${h}    IN    @{HUBS}
        ${cfg}=    Show    ${h}    show running-config all | section interface Tunnel0
        Should Contain    ${cfg}    ip nhrp redirect
        Should Contain    ${cfg}    ip nhrp map multicast dynamic
        ${dm}=    Show    ${h}    show dmvpn | begin Interface
        Should Contain    ${dm}    Type:Hub
        FOR    ${s}    IN    @{SPOKES}
            Should Match Regexp    ${dm}    (?m)^\\s*\\d+\\s+${ROUTERS}[${s}][nbma]\\s+${ROUTERS}[${s}][tunnel]\\s+UP\\s+\\S+\\s+D
        END
    END

The hubs know each other statically, not as clients, and the tunnel between them is protected
    [Documentation]    A static NHRP map is never registered, so its state reads NHRP rather than UP; what proves the
    ...                hub-to-hub path is the IPsec session to the other hub's NBMA address (and 04's iBGP session).
    FOR    ${h}    IN    @{HUBS}
        ${dm}=    Show    ${h}    show dmvpn | begin Interface
        ${cs}=    Show    ${h}    show crypto session brief
        FOR    ${o}    IN    @{HUBS}
            Continue For Loop If    '${h}' == '${o}'
            Should Match Regexp    ${dm}    (?m)^\\s*\\d+\\s+${ROUTERS}[${o}][nbma]\\s+${ROUTERS}[${o}][tunnel]\\s+(UP|NHRP)\\s+\\S+\\s+S\\s*$
            Should Match Regexp    ${cs}    (?m)^${ROUTERS}[${o}][nbma]\\s+Tu0\\s+.*\\sUA\\s*$
        END
    END

Every customer has all three hubs as NHS, with shortcuts enabled
    FOR    ${s}    IN    @{C8K_SPOKES}
        ${cfg}=    Show    ${s}    show running-config all | section interface Tunnel0
        Should Contain    ${cfg}    ip nhrp shortcut
        ${nhs}=    Show    ${s}    show ip nhrp nhs detail
        FOR    ${h}    IN    @{HUBS}
            Should Match Regexp    ${nhs}    (?m)^\\s*${ROUTERS}[${h}][tunnel]\\s+RE\\s+NBMA Address: ${ROUTERS}[${h}][nbma]
        END
        ${dm}=    Show    ${s}    show dmvpn | begin Interface
        Should Contain    ${dm}    Type:Spoke
        FOR    ${h}    IN    @{HUBS}
            Should Match Regexp    ${dm}    (?m)^\\s*\\d+\\s+${ROUTERS}[${h}][nbma]\\s+${ROUTERS}[${h}][tunnel]\\s+UP\\s+\\S+\\s+S
        END
    END

IKEv2 SAs are READY from every customer to every hub, with the modelled crypto
    FOR    ${s}    IN    @{C8K_SPOKES}
        ${sa}=    Show    ${s}    show crypto ikev2 sa
        FOR    ${h}    IN    @{HUBS}
            Should Match Regexp    ${sa}    (?m)^\\d+\\s+${ROUTERS}[${s}][nbma]/500\\s+${ROUTERS}[${h}][nbma]/500\\s+none/none\\s+READY
        END
        Should Contain    ${sa}    Encr: AES-CBC, keysize: 256, PRF: SHA256, Hash: SHA256, DH Grp:14, Auth sign: PSK
    END
    FOR    ${r}    IN    @{C8K}
        ${ipsec}=    Show    ${r}    show crypto ipsec sa | include pkts (encaps|decaps)
        Should Match Regexp    ${ipsec}    \#pkts encaps: [1-9]\\d*
        Should Match Regexp    ${ipsec}    \#pkts decaps: [1-9]\\d*
    END

Customer-to-customer traffic builds a dynamic shortcut tunnel
    [Documentation]    Phase 3: the first packets go through a hub, which sends an NHRP redirect; the customer resolves
    ...                the other customer's NBMA address, brings up a direct IPsec tunnel, and the next traceroute's
    ...                first hop is the other customer's tunnel address.
    FOR    ${pair}    IN    cust1:cust2    cust2:cust3    cust3:cust1
        ${a}    ${b}=    Split String    ${pair}    :
        ${p}=    Show    ${a}    ping ${ROUTERS}[${b}][lan_ip] source ${ROUTERS}[${a}][lan_ip] repeat 10
        Should Match Regexp    ${p}    Success rate is (100|90|80|70) percent
        Wait Until Keyword Succeeds    45s    3s    Customer Has Dynamic Peer    ${a}    ${b}
        ${tr}=    Show    ${a}    traceroute ${ROUTERS}[${b}][lan_ip] source ${ROUTERS}[${a}][lan_ip] numeric probe 1 timeout 2
        Should Match Regexp    ${tr}    (?m)^\\s*1\\s+${ROUTERS}[${b}][tunnel]\\s    msg=${a} -> ${b} still goes via a hub
        ${sa}=    Show    ${a}    show crypto ikev2 sa
        Should Match Regexp    ${sa}    (?m)^\\d+\\s+${ROUTERS}[${a}][nbma]/500\\s+${ROUTERS}[${b}][nbma]/500\\s+none/none\\s+READY
    END

*** Keywords ***
Customer Has Dynamic Peer
    [Arguments]    ${customer}    ${peer}
    ${dm}=    Show    ${customer}    show dmvpn | begin Interface
    Should Match Regexp    ${dm}    (?m)^\\s*\\d+\\s+${ROUTERS}[${peer}][nbma]\\s+${ROUTERS}[${peer}][tunnel]\\s+UP\\s+\\S+\\s+D
