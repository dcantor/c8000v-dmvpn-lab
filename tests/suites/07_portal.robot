*** Settings ***
Documentation     The provisioning portal (:8094): it answers, its live view agrees with the routers, it allocates the next
...               customer the lab's way, and it refuses one that would collide.
Resource          ../resources/common.resource
Library           RequestsLibrary
Force Tags        portal
Suite Setup       Create Session    portal    http://127.0.0.1:8094    timeout=180

*** Test Cases ***
The portal answers and says the cloud is healthy
    ${r}=    GET On Session    portal    /api/state    params=refresh=true
    Should Be True    ${r.json()}[health][ok]    msg=${r.json()}[health][problems]
    ${cust}=    Get Length    ${r.json()}[customers]
    ${want}=    Get Length    ${SPOKES}
    Should Be Equal As Integers    ${cust}    ${want}

The live view agrees with the routers: every customer registered with every hub
    ${r}=    GET On Session    portal    /api/state
    FOR    ${s}    IN    @{SPOKES}
        ${nhs}=    Set Variable    ${r.json()}[cloud][${s}][nhs_up]
        Lists Should Be Equal    ${nhs}    ${{sorted($HUBS)}}
    END

The next customer is allocated the lab's way and validates cleanly
    ${r}=    GET On Session    portal    /api/customers/suggest
    ${s}=    Set Variable    ${r.json()}
    ${n}=    Get Length    ${SPOKES}
    Should Be Equal    ${s}[name]    cust${n + 1}
    Should Be Equal As Integers    ${s}[t_idx]    ${10 + ${n} + 1}
    Should Be Equal    ${s}[tunnel_ip]    172.28.0.${s}[t_idx]
    Should Be Equal    ${s}[wan_prefix]    100.70.${s}[t_idx].0/30
    ${v}=    POST On Session    portal    /api/customers/validate    json=${s}
    Should Be Empty    ${v.json()}[problems]

A customer that would collide with the running lab is refused
    ${r}=    GET On Session    portal    /api/customers/suggest
    ${s}=    Set Variable    ${r.json()}
    Set To Dictionary    ${s}    name=cust1    mgmt_ip=${ROUTERS}[cust1][host]    t_idx=${11}    lan=${ROUTERS}[cust1][lan]
    ${v}=    POST On Session    portal    /api/customers/validate    json=${s}
    ${problems}=    Catenate    SEPARATOR=\n    @{v.json()}[problems]
    Should Contain    ${problems}    cust1 already exists
    Should Contain    ${problems}    index 11 is taken
    Should Contain    ${problems}    overlaps ${ROUTERS}[cust1][lan]
