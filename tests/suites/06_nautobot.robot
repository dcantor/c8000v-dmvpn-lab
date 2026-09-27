*** Settings ***
Documentation     Nautobot is the source of truth: it holds the whole lab, and rendering from it produces exactly the
...               configuration lab.conf does — every day-0, the provider, and the NAC model.
Resource          ../resources/common.resource
Force Tags        nautobot

*** Test Cases ***
Every node of the lab is a device in Nautobot, under its libvirt name
    ${names}=    Nautobot Device Names    c8000v-dmvpn-lab
    FOR    ${n}    IN    @{ALL_NODES}
        List Should Contain Value    ${names}    ${DOMAIN_PREFIX}${n}
    END
    ${count}=    Get Length    ${names}
    ${want}=    Get Length    ${ALL_NODES}
    Should Be Equal As Integers    ${count}    ${want}    msg=Nautobot holds devices the lab does not have

The model is in sync with lab.conf
    ${rc}=    Lab Sh Exit Code    nautobot    seed    --check
    Should Be Equal As Integers    ${rc}    0    msg=seed --check would change Nautobot: the model has drifted from lab.conf

Rendering from Nautobot equals rendering from lab.conf, byte for byte
    ${rc}=    Lab Sh Exit Code    nautobot    render    --check
    Should Be Equal As Integers    ${rc}    0    msg=Nautobot's rendering differs from the committed configuration

Every customer company is a tenant with its details, on the customer's router and LAN host
    ${tenants}=    Nautobot Customer Tenants    c8000v-dmvpn-lab customers
    ${n}=    Get Length    ${tenants}
    ${want}=    Get Length    ${SPOKES}
    Should Be Equal As Integers    ${n}    ${want}    msg=one tenant per customer, no strays
    FOR    ${c}    IN    @{SPOKES}
        ${cu}=    Set Variable    ${COMPANIES}[${c}]
        Should Not Be Equal    ${cu}    ${None}    msg=${c} has no entry in customers.json
        Dictionary Should Contain Key    ${tenants}    ${cu}[company]
        ${t}=    Set Variable    ${tenants}[${cu}[company]]
        FOR    ${field}    IN    industry    address    phone    contact    email    account
            Should Be Equal    ${t}[fields][customer_${field}]    ${cu}[${field}]
        END
        Lists Should Be Equal    ${t}[devices]    ${{sorted([$DOMAIN_PREFIX + $c, $DOMAIN_PREFIX + $ROUTERS[$c]["host_vm"]])}}
    END

Every application is a Virtual Server with its VIP at each hub that hosts it
    ${nb}=    Nautobot Applications
    ${count}=    Set Variable    ${0}
    FOR    ${app}    IN    @{APPLICATIONS}
        FOR    ${hub}    IN    @{app}[hubs]
            ${key}=    Set Variable    ${app}[id]@${DOMAIN_PREFIX}${hub}
            Dictionary Should Contain Key    ${nb}[vs]    ${key}    msg=no Virtual Server for ${app}[id] at ${hub}
            ${v}=    Set Variable    ${nb}[vs][${key}]
            Should Be Equal    ${v}[vip]    ${app}[vips][${hub}]
            Should Be Equal As Integers    ${v}[port]    ${app}[port]
            Should Be Equal    ${v}[protocol]    ${app}[protocol]
            Should Be Equal    ${v}[url]    ${app}[url]
            Should Be Equal    ${v}[name]    ${app}[name]
            ${count}=    Evaluate    ${count} + 1
        END
    END
    ${have}=    Get Length    ${nb}[vs]
    Should Be Equal As Integers    ${have}    ${count}    msg=Virtual Servers Nautobot has that the catalogue does not

Each customer subscribes to exactly its applications
    ${nb}=    Nautobot Applications
    FOR    ${c}    IN    @{SPOKES}
        ${cu}=    Set Variable    ${COMPANIES}[${c}]
        ${want}=    Evaluate    sorted($cu.get("applications") or [])
        ${have}=    Evaluate    $nb["subs"].get($cu["company"], [])
        Lists Should Be Equal    ${have}    ${want}
    END
