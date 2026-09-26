*** Settings ***
Documentation     Configuration compliance: the renders are current, and the routers match the Network-as-Code model.
Resource          ../resources/common.resource
Suite Teardown    Suite Teardown Close Connections

*** Test Cases ***
The rendered configuration is current with lab.conf
    ${rc}=    Rendered Configs Are Current
    Should Be Equal As Integers    ${rc}    0    msg=run tools/gen_configs.py: the committed renders are stale

Terraform plan reports no drift from the NAC data model
    [Tags]    nac
    ${rc}=    Terraform Plan Exit Code
    Should Be Equal As Integers    ${rc}    0    msg=terraform plan exit code ${rc} (0 = in sync, 2 = drift, 1 = error)

Every line of each CLI template is in the running configuration
    [Documentation]    Terraform cannot see drift inside a raw CLI template (Tunnel0, the hub listen range), so compare
    ...                every rendered line with what the router is running.
    ${model}=    Read Lab File    nac/rendered-model.yaml
    FOR    ${r}    IN    @{C8K}
        ${run}=    Show    ${r}    show running-config all | section ^interface Tunnel0|^router bgp
        ${tun}=    Show    ${r}    show running-config interface Tunnel0
        FOR    ${line}    IN    ip address ${ROUTERS}[${r}][tunnel] 255.255.255.0    ip nhrp authentication    ip nhrp holdtime ${NHRP_HOLDTIME}
        ...    ip mtu 1400    ip tcp adjust-mss 1360    tunnel key ${TUNNEL_KEY}
            Should Contain    ${tun}    ${line}
        END
        IF    '${ROUTERS}[${r}][role]' == 'hub'
            Should Contain    ${run}    bgp listen range ${OVERLAY} peer-group CUSTOMERS
            Should Contain    ${run}    neighbor CUSTOMERS route-reflector-client
        END
    END
