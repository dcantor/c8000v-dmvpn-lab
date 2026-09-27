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

The Network map page is served, with everything it draws present in the live state
    [Documentation]    The map is drawn in the browser from /api/state: every router's access link and LAN port, each
    ...                customer's registrations and shortcuts, the hubs' IPsec peers and the provider's sessions.
    ${page}=    GET On Session    portal    /
    Should Contain    ${page.text}    data-view="map"
    Should Contain    ${page.text}    id="map"
    ${r}=    GET On Session    portal    /api/state
    ${s}=    Set Variable    ${r.json()}
    FOR    ${n}    IN    @{HUBS}    @{SPOKES}
        Dictionary Should Contain Key    ${s}[nodes][${n}][wan]    peer_port
        Dictionary Should Contain Key    ${s}[cloud][${n}]    sa_peers
    END
    FOR    ${c}    IN    @{SPOKES}
        Dictionary Should Contain Key    ${s}[nodes][${c}][lan_port]    peer_ip
        Dictionary Should Contain Key    ${s}[cloud][${c}]    nhs_up
        Dictionary Should Contain Key    ${s}[cloud][${c}]    shortcuts
    END
    Dictionary Should Contain Key    ${s}[provider_state][${PROVIDER}]    sessions

Every LAN host pings every other one from the Network map, and only hosts can be pinged
    FOR    ${h}    IN    @{HOSTS}
        FOR    ${o}    IN    @{HOSTS}
            Continue For Loop If    '${h}' == '${o}'
            ${r}=    GET On Session    portal    /api/hosts/${h}/ping    params=target=${o}
            Should Be True    ${r.json()}[ok]    msg=${h} cannot reach ${o}: ${r.json()}[output]
            Should Be Equal    ${r.json()}[address]    ${HOST_VMS}[${o}][lan_ip]
        END
    END
    ${bad}=    GET On Session    portal    /api/hosts/${HOSTS}[0]/ping    params=target=${HUBS}[0]    expected_status=404
    ${self}=    GET On Session    portal    /api/hosts/${HOSTS}[0]/ping    params=target=${HOSTS}[0]    expected_status=400

Each customer's view of the Network map downloads as a two-page PDF
    [Documentation]    The portal's headless Chrome renders the map in print mode, focused on the customer: page one the
    ...                company and its view of the network, page two its applications and the technical details.
    FOR    ${c}    IN    @{SPOKES}
        ${r}=    GET On Session    portal    /api/customers/${c}/map.pdf
        Should Be Equal    ${r.headers}[content-type]    application/pdf
        Should Contain    ${r.headers}[content-disposition]    ${c}-network-map-
        Should Start With    ${r.content}    ${{b"%PDF"}}
        ${pages}=    Pdf Page Count    ${r.content}
        Should Be Equal As Integers    ${pages}    2    msg=${c}'s map PDF runs to ${pages} pages
    END
    GET On Session    portal    /api/customers/${HUBS}[0]/map.pdf    expected_status=404

The portal shows each customer's company, and proposes a complete one for the next customer
    ${r}=    GET On Session    portal    /api/state
    FOR    ${c}    IN    @{SPOKES}
        Dictionaries Should Be Equal    ${r.json()}[nodes][${c}][customer]    ${COMPANIES}[${c}]
    END
    ${s}=    GET On Session    portal    /api/customers/suggest
    FOR    ${field}    IN    company    industry    address    phone    contact    email    account
        Should Not Be Empty    ${s.json()}[customer][${field}]
    END
    Should Match Regexp    ${s.json()}[customer][phone]    555-01\\d\\d$    msg=fictional numbers only
    Should End With    ${s.json()}[customer][email]    .example

The live state carries the application catalogue the map draws from
    ${r}=    GET On Session    portal    /api/state
    ${n}=    Get Length    ${r.json()}[applications]
    ${want}=    Get Length    ${APPLICATIONS}
    Should Be Equal As Integers    ${n}    ${want}

Lab Tools lists every node's access details and every tool, and the tools answer
    ${page}=    GET On Session    portal    /
    Should Contain    ${page.text}    data-view="tools"
    ${r}=    GET On Session    portal    /api/lab-tools
    ${t}=    Set Variable    ${r.json()}
    ${by}=    Evaluate    {n["name"]: n for n in $t["nodes"]}
    FOR    ${n}    IN    @{ALL_NODES}
        Dictionary Should Contain Key    ${by}    ${n}
        Should Be Equal    ${by}[${n}][mgmt_ip]    ${MGMT_IPS}[${n}]
        Should Be Equal    ${by}[${n}][vm]    ${DOMAIN_PREFIX}${n}
        Should Match Regexp    ${by}[${n}][console]    ^127\\.0\\.0\\.1:55\\d\\d$
        Should Not Be Empty    ${by}[${n}][password]
    END
    FOR    ${tool}    IN    @{t}[tools]
        Continue For Loop If    '${tool}[url]'.startswith('https://github.com')
        ${code}=    Evaluate    __import__("requests").get($tool["url"], timeout=15, allow_redirects=False).status_code
        Should Be True    ${code} < 400    msg=${tool}[name] (${tool}[url]) answers ${code}
    END
