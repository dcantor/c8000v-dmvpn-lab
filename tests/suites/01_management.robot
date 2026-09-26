*** Settings ***
Documentation     Every VM is this lab's, running, reachable on the OOB network, and carries its modelled identity.
Resource          ../resources/common.resource
Suite Teardown    Suite Teardown Close Connections

*** Test Cases ***
Every VM is running under its lab-prefixed libvirt name
    FOR    ${n}    IN    @{ALL_NODES}
        ${st}=    Domain State    ${DOMAIN_PREFIX}${n}
        Should Be Equal    ${st}    running    msg=${DOMAIN_PREFIX}${n} is ${st}
    END

Every node answers SSH on its OOB address
    FOR    ${n}    IN    @{ALL_NODES}
        Tcp Port Should Be Open    ${MGMT_IPS}[${n}]    22
    END

Every C8000v runs 17.15 with the crypto feature set and its modelled hostname
    FOR    ${r}    IN    @{C8K}
        ${v}=    Show    ${r}    show version
        Should Contain    ${v}    Version 17.15
        Should Match Regexp    ${v}    (?i)License Level:\\s+network-advantage
        ${h}=    Restconf Get    ${ROUTERS}[${r}][host]    Cisco-IOS-XE-native:native/hostname
        Should Be Equal    ${h}[Cisco-IOS-XE-native:hostname]    ${r}
    END

Management is isolated in Mgmt-vrf and the vty lines are hardened
    FOR    ${r}    IN    @{C8K}
        ${gi1}=    Show    ${r}    show running-config interface GigabitEthernet1
        Should Contain    ${gi1}    vrf forwarding Mgmt-vrf
        Should Contain    ${gi1}    ip address ${ROUTERS}[${r}][host] 255.255.255.0
        ${vty}=    Show    ${r}    show running-config | section line vty
        Should Contain    ${vty}    access-class ${MGMT_ACL} in vrf-also
        Should Contain    ${vty}    transport input ssh
        ${b}=    Show    ${r}    show banner motd
        Should Contain    ${b}    ${BANNER_TEXT}
    END

No C8000v has a route outside Mgmt-vrf that it was not given by BGP or a connected interface
    [Documentation]    No static or default routes in the global table: the underlay is eBGP, the overlay iBGP.
    FOR    ${r}    IN    @{C8K}
        ${rt}=    Show    ${r}    show ip route static
        Should Not Match Regexp    ${rt}    (?m)^S
        ${d}=    Show    ${r}    show ip route 0.0.0.0
        Should Contain    ${d}    not in table
    END

The provider runs VyOS with its modelled identity
    ${v}=    Provider    show version
    Should Contain    ${v}    VyOS
    ${h}=    Provider    show host name
    Should Be Equal    ${h.strip()}    ${PROVIDER}

Every LAN host is up with its modelled LAN address
    FOR    ${h}    IN    @{HOSTS}
        ${res}=    On Host    ${h}    hostname; ip -4 -o addr show eth1
        Should Be Equal As Integers    ${res}[0]    0
        Should Contain    ${res}[1]    ${h}
        Should Contain    ${res}[1]    ${HOST_VMS}[${h}][lan_ip]/24
    END
