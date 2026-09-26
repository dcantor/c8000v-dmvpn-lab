*** Settings ***
Documentation     Shared settings for the c8000v-dmvpn-lab suites.
Library           LabLib.py
Variables         lab_vars.py
Library           Collections
Library           String

*** Keywords ***
Suite Teardown Close Connections
    Close All Connections

Router Host
    [Arguments]    ${r}
    RETURN    ${ROUTERS}[${r}][host]

Show
    [Documentation]    Run a show command on a C8000v by name.
    [Arguments]    ${r}    ${command}    ${timeout}=60
    ${host}=    Router Host    ${r}
    ${out}=    Run Command    ${host}    ${command}    ${timeout}
    RETURN    ${out}

Provider
    [Documentation]    Run an operational command on the VyOS provider.
    [Arguments]    ${command}
    ${out}=    Vyos Command    ${PROVIDER_HOST}    ${command}
    RETURN    ${out}

On Host
    [Documentation]    Run a shell command on an Alpine LAN host; returns [rc, output].
    [Arguments]    ${h}    ${command}    ${timeout}=60
    ${res}=    Host Command    ${HOST_VMS}[${h}][host]    ${command}    ${timeout}
    RETURN    ${res}
