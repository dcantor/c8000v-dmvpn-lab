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
