*** Settings ***
Documentation     The provisioning portal (:8094): it answers, its live view agrees with the routers, it allocates the next
...               customer the lab's way, and it refuses one that would collide.
Resource          ../resources/common.resource
Library           RequestsLibrary
Force Tags        portal
Suite Setup       Sign In

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
    Should Be Equal    ${s}[name]    cust${NEXT_CUSTOMER}    msg=the next name is the highest customer number plus one (gaps are not reused)
    Should Be Equal As Integers    ${s}[t_idx]    ${10 + ${NEXT_CUSTOMER}}
    Should Be Equal    ${s}[tunnel_ip]    172.28.0.${s}[t_idx]
    Should Be Equal    ${s}[wan_prefix]    100.70.${s}[t_idx].0/30
    ${v}=    POST On Session    portal    /api/customers/validate    json=${s}
    Should Be Empty    ${v.json()}[problems]

A customer that would collide with the running lab is refused
    ${r}=    GET On Session    portal    /api/customers/suggest
    ${s}=    Set Variable    ${r.json()}
    ${c}=    Set Variable    ${SPOKES}[0]                    # whichever customer exists first
    Set To Dictionary    ${s}    name=${c}    mgmt_ip=${ROUTERS}[${c}][host]    t_idx=${ROUTERS}[${c}][t_idx]    lan=${ROUTERS}[${c}][lan]
    ${v}=    POST On Session    portal    /api/customers/validate    json=${s}
    ${problems}=    Catenate    SEPARATOR=\n    @{v.json()}[problems]
    Should Contain    ${problems}    ${c} already exists
    Should Contain    ${problems}    index ${ROUTERS}[${c}][t_idx] is taken
    Should Contain    ${problems}    overlaps ${ROUTERS}[${c}][lan]

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

Each customer's view of the Network map downloads as a PDF: the map, the applications, the service report
    [Documentation]    The portal's headless Chrome renders the map in print mode, focused on the customer: page one the
    ...                company and its view of the network, page two its applications and the technical details, page
    ...                three this month's service report, page four its applications' availability (if it has any).
    FOR    ${c}    IN    @{SPOKES}
        ${r}=    GET On Session    portal    /api/customers/${c}/map.pdf
        Should Be Equal    ${r.headers}[content-type]    application/pdf
        Should Contain    ${r.headers}[content-disposition]    ${c}-network-map-
        Should Start With    ${r.content}    ${{b"%PDF"}}
        ${pages}=    Pdf Page Count    ${r.content}
        ${want}=    Evaluate    4 if $COMPANIES[$c].get('applications') else 3
        Should Be Equal As Integers    ${pages}    ${want}    msg=${c}'s map PDF runs to ${pages} pages
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

Show configuration returns each node's own configuration, read live
    FOR    ${n}    IN    @{C8K}
        ${r}=    GET On Session    portal    /api/config/${n}
        Should Be Equal    ${r.json()}[command]    show running-config
        Should Contain    ${r.json()}[output]    hostname ${n}
        Should Contain    ${r.json()}[output]    interface Tunnel0
    END
    ${r}=    GET On Session    portal    /api/config/${PROVIDER}
    Should Contain    ${r.json()}[output]    set system host-name '${PROVIDER}'
    FOR    ${h}    IN    @{HOSTS}
        ${r}=    GET On Session    portal    /api/config/${h}
        Should Contain    ${r.json()}[output]    \# hostname: ${h}
        Should Contain    ${r.json()}[output]    ${HOST_VMS}[${h}][lan_ip]
    END
    GET On Session    portal    /api/config/no-such-node    expected_status=404

Runs know how long their steps take, for the progress bars' estimates
    ${r}=    GET On Session    portal    /api/runs/estimates
    ${e}=    Set Variable    ${r.json()}[steps]
    FOR    ${step}    IN    validate    labconf    vm    bootstrap    nac    provider    nautobot    verify
        Dictionary Should Contain Key    ${e}    */*/${step}    msg=no finished run has a ${step} step to estimate from
        Should Be True    ${e}[*/*/${step}] >= 0
    END

A traceroute between two hosts names every hop and says whether it crossed a hub
    Skip If    len($HOSTS) < 2    needs two hosts
    ${r}=    GET On Session    portal    /api/hosts/${HOST_PAIR}[0]/traceroute    params=target=${HOST_PAIR}[1]
    ${t}=    Set Variable    ${r.json()}
    Should Be True    ${t}[reached]    msg=${t}[output]
    Should Contain Any    ${t}[kind]    hub    direct
    Should Be Equal    ${t}[path][0]    ${HOST_PAIR}[0]
    Should Be Equal    ${t}[path][1]    ${HOST_VMS}[${HOST_PAIR}[0]][router]
    Should Be Equal    ${t}[path][-1]    ${HOST_PAIR}[1]
    Should Be Equal    ${t}[hops][0][node]    ${HOST_VMS}[${HOST_PAIR}[0]][router]
    GET On Session    portal    /api/hosts/${HOST_PAIR}[0]/traceroute    params=target=${HUBS}[0]    expected_status=404

Clearing a shortcut sends the next packets through a hub, and traffic builds the shortcut again
    [Documentation]    What "watch the shortcut form" shows on the map. A pair of VyOS customers, or a customer that
    ...                prefers a hub, routes through a hub until phase 3 resolves the shortcut; the first trace after
    ...                the reset crosses a hub, and a trace after some traffic goes direct.
    ${pair}=    Set Variable    ${HUB_FIRST_PAIR}
    Skip If    not $pair    no customer routes through a hub before its shortcut forms (no VyOS customer, no preference)
    ${r}=    POST On Session    portal    /api/hosts/${pair}[0]/path/reset    params=target=${pair}[1]
    Length Should Be    ${r.json()}[routers]    2
    ${cold}=    GET On Session    portal    /api/hosts/${pair}[0]/traceroute    params=target=${pair}[1]
    Should Be Equal    ${cold.json()}[kind]    hub    msg=right after the reset: ${cold.json()}[output]
    ${warm}=    GET On Session    portal    /api/hosts/${pair}[0]/traceroute    params=target=${pair}[1]&warm=true
    Should Be Equal    ${warm.json()}[kind]    direct    msg=after traffic: ${warm.json()}[output]

Every router runs what the model says: a drift check finds nothing
    [Documentation]    A drift run: Nautobot vs lab.conf, terraform plan for the C8000vs and their CLI templates against
    ...                the running configuration, the rendered set lines for VyOS. The report drives the map's badges.
    ${d}=    POST On Session    portal    /api/drift/check    timeout=600
    Should Be True    ${d.json()}[ok]    msg=drift on ${d.json()}[drifted]: ${d.json()}[nodes]
    FOR    ${n}    IN    @{DMVPN}    ${PROVIDER}
        Should Be Equal    ${d.json()}[nodes][${n}][status]    ok
    END
    ${m}=    GET On Session    portal    /metrics
    Should Contain    ${m.text}    lab_config_drift{

Every customer's service levels are measured and reported from VictoriaMetrics
    ${m}=    GET On Session    portal    /metrics
    FOR    ${c}    IN    @{SPOKES}
        FOR    ${h}    IN    @{HUBS}
            Should Contain    ${m.text}    lab_sla_loss_ratio{lab="c8000v-dmvpn-lab",customer="${c}",hub="${h}"}
        END
    END
    ${r}=    GET On Session    portal    /api/customers/${SPOKES}[0]/sla    params=window=24h
    ${s}=    Set Variable    ${r.json()}
    Should Be Equal    ${s}[customer]    ${SPOKES}[0]
    Lists Should Be Equal    ${{sorted($s['hubs'])}}    ${{sorted($HUBS)}}
    Should Be True    ${s}[availability] is not None and 0 <= ${s}[availability] <= 1
    Should Be True    ${s}[coverage] > 0    msg=nothing recorded for ${SPOKES}[0] in the last 24 hours
    GET On Session    portal    /api/customers/${SPOKES}[0]/sla    params=window=never    expected_status=400
    GET On Session    portal    /api/customers/${HUBS}[0]/sla    expected_status=404

A change to a customer is planned before anything happens, and a bad one is refused
    ${c}=    Set Variable    ${SPOKES}[0]
    ${cur}=    GET On Session    portal    /api/customers/${c}
    Should Be Equal    ${cur.json()}[lan]    ${ROUTERS}[${c}][lan]
    ${other}=    Evaluate    [h for h in $HUBS if h != $cur.json().get('prefer_hub')][0]
    ${v}=    POST On Session    portal    /api/customers/${c}/modify/validate    json=${{{"prefer_hub": $other}}}
    Should Be Empty    ${v.json()}[problems]
    Should Be Equal    ${v.json()}[plan][changes][0][field]    prefer_hub
    Should Not Be True    ${v.json()}[plan][rebuild]
    ${lan}=    Set Variable    ${ROUTERS}[${SPOKES}[-1]][lan]
    ${bad}=    POST On Session    portal    /api/customers/${c}/modify/validate    json=${{{"lan": $lan, "platform": "junos"}}}
    ${p}=    Catenate    SEPARATOR=\n    @{bad.json()}[problems]
    Should Contain    ${p}    overlaps
    Should Contain    ${p}    junos
    ${none}=    POST On Session    portal    /api/customers/${c}/modify/validate    json=${{{}}}
    Should Contain    ${none.json()}[problems]    nothing to change

A backup holds the whole lab state, and uploading it back plans no change
    ${r}=    GET On Session    portal    /api/backup    params=running=false
    Should Be Equal    ${r.headers}[content-type]    application/gzip
    ${names}=    Evaluate    [m.split('/', 1)[1] for m in __import__('tarfile').open(fileobj=__import__('io').BytesIO($r.content)).getnames()]
    FOR    ${f}    IN    manifest.json    intent/lab.conf    intent/customers.json    intent/applications.json    renders/nac/data/devices.nac.yaml    nautobot/model.json
        List Should Contain Value    ${names}    ${f}
    END
    ${u}=    POST On Session    portal    /api/backups    data=${r.content}    headers=${{{"content-type": "application/gzip"}}}
    Should Be Empty    ${u.json()}[problems]
    Should Be True    ${u.json()}[plan][same_intent]
    Should Be Empty    ${u.json()}[plan][removed]
    Should Be Empty    ${u.json()}[plan][added]
    Should Be Empty    ${u.json()}[plan][changed]
    ${bad}=    POST On Session    portal    /api/backups    data=not a backup    headers=${{{"content-type": "application/gzip"}}}    expected_status=400

Change control: a covered change waits for someone else's approval, and a simulated failure goes in and comes out
    [Documentation]    The policy covers simulated failures (a lab default): filing one answers 202 with a change request;
    ...                the requester cannot approve it (four eyes); another name can, and the fault goes in. Restoring
    ...                never needs approval. The failure is harmless: a dual-homed customer's backup circuit.
    ${p}=    GET On Session    portal    /api/policy
    Skip If    not ($p.json()['approval']['enabled'] and 'fault' in $p.json()['approval']['covers'])    the policy does not cover failures
    Skip If    not $DUAL_SPOKES    no dual-homed customer: no harmless failure to simulate
    ${target}=    Set Variable    ${DUAL_SPOKES}[0]:wan2
    ${r}=    POST On Session    portal    /api/faults    json=${{{"kind": "site-wan", "target": $target, "reason": "suite 07"}}}    expected_status=202
    ${cr}=    Set Variable    ${r.json()}[change][id]
    Should Be Equal    ${r.json()}[change][status]    pending
    Should Be Equal    ${r.json()}[change][requested_by]    operator
    POST On Session    portal    /api/changes/${cr}/approve    json=${{{}}}    expected_status=403
    ${ok}=    POST On Session    approver    /api/changes/${cr}/approve    json=${{{"comment": "suite 07"}}}
    Should Be Equal    ${ok.json()}[status]    started    msg=${ok.json()}
    ${fid}=    Set Variable    ${ok.json()}[fault_id]
    ${f}=    GET On Session    portal    /api/faults
    Should Be Equal    ${f.json()}[active][0][id]    ${fid}
    ${st}=    GET On Session    portal    /api/state
    Length Should Be    ${st.json()}[faults]    1
    ${back}=    DELETE On Session    portal    /api/faults/${fid}
    Should Contain    ${back.json()}[undone]    delete
    ${f}=    GET On Session    portal    /api/faults
    Should Be Empty    ${f.json()}[active]
    ${again}=    POST On Session    approver    /api/changes/${cr}/approve    json=${{{}}}    expected_status=409

The failure catalogue offers every kind of failure, and the failover measurements are listed
    ${f}=    GET On Session    portal    /api/faults
    ${kinds}=    Evaluate    sorted(k['kind'] for k in $f.json()['catalog'])
    Lists Should Be Equal    ${kinds}    ${{sorted(["hub-down", "hub-wan", "provider-down", "site-wan", "tunnel-down"])}}
    ${hubs}=    Evaluate    [o['id'] for k in $f.json()['catalog'] if k['kind'] == 'hub-down' for o in k['options']]
    Lists Should Be Equal    ${hubs}    ${HUBS}
    ${m}=    GET On Session    portal    /api/failover
    FOR    ${x}    IN    @{m.json()}
        Should Contain Any    ${x}[kind]    hub-down    hub-wan    provider-down    site-wan    tunnel-down
        Dictionary Should Contain Key    ${x}[summary]    by_verdict
    END

A customer's own link shows its service and nothing of any other customer
    ${c}=    Set Variable    ${SPOKES}[0]
    ${l}=    GET On Session    portal    /api/customers/${c}/portal-link
    ${page}=    GET On Session    portal    ${l.json()}[path]
    Should Contain    ${page.text}    customerMode
    ${token}=    Evaluate    $l.json()['path'].split('/')[-1]
    ${v}=    GET On Session    portal    /api/c/${token}/state
    Lists Should Be Equal    ${v.json()}[customers]    ${{[$c]}}
    Should Be Equal    ${v.json()}[customer]    ${c}
    FOR    ${o}    IN    @{SPOKES}
        Continue For Loop If    '${o}' == '${c}'
        Should Not Contain    ${v.text}    "${o}"    msg=${o} leaks into ${c}'s view
        Should Not Contain    ${v.text}    ${COMPANIES}[${o}][company]
        Should Not Contain    ${v.text}    ${ROUTERS}[${o}][lan]
    END
    ${s}=    GET On Session    portal    /api/c/${token}/sla    params=window=24h
    Should Be Equal    ${s.json()}[customer]    ${c}
    GET On Session    portal    /api/c/not-a-token/state    expected_status=404
    GET On Session    portal    /c/not-a-token    expected_status=404

Nobody gets in without an account, and a role only does what it may
    Create Session    anon    http://127.0.0.1:8094    timeout=60
    GET On Session    anon    /api/state    expected_status=401
    GET On Session    anon    /metrics
    GET On Session    anon    /api/version
    POST On Session    anon    /api/auth/login    json=${{{"username": "operator", "password": "wrong"}}}    expected_status=401
    ${me}=    GET On Session    portal    /api/auth/me
    Should Be Equal    ${me.json()}[username]    operator
    Create Session    viewer    http://127.0.0.1:8094    timeout=60
    POST On Session    viewer    /api/auth/login    json=${PORTAL_LOGIN}[viewer]
    GET On Session    viewer    /api/state
    POST On Session    viewer    /api/runs    json=${{{"mode": "drift"}}}    expected_status=403
    GET On Session    viewer    /api/auth/users    expected_status=403
    GET On Session    portal    /api/auth/users    expected_status=403
    POST On Session    portal    /api/policy    json=${{{}}}    expected_status=403

Maintenance mutes a customer's nodes and tells the customer, and ends
    ${c}=    Set Variable    ${SPOKES}[-1]
    ${m}=    POST On Session    portal    /api/maintenance    json=${{{"summary": "suite 07: a maintenance", "customers": [$c]}}}
    ${met}=    GET On Session    portal    /metrics
    Should Contain    ${met.text}    lab_maintenance{lab="c8000v-dmvpn-lab",router="${c}"} 1
    Should Contain    ${met.text}    lab_maintenance_customer{lab="c8000v-dmvpn-lab",customer="${c}"} 1
    ${st}=    GET On Session    portal    /api/state
    Length Should Be    ${st.json()}[maintenance]    1
    ${l}=    GET On Session    portal    /api/customers/${c}/portal-link
    ${token}=    Evaluate    $l.json()['path'].split('/')[-1]
    ${v}=    GET On Session    portal    /api/c/${token}/state
    Length Should Be    ${v.json()}[maintenance_now]    1
    DELETE On Session    portal    /api/maintenance/${m.json()}[id]
    ${met}=    GET On Session    portal    /metrics
    Should Contain    ${met.text}    lab_maintenance{lab="c8000v-dmvpn-lab",router="${c}"} 0

Every changing job keeps each router's configuration before and after
    ${h}=    GET On Session    portal    /api/config-history
    Skip If    not $h.json()    no changing job has run since 0.20.0
    ${job}=    Set Variable    ${h.json()}[0][job]
    ${d}=    GET On Session    portal    /api/runs/${job}/config
    FOR    ${r}    IN    @{d.json()}
        Should Contain    ${d.json()}[${r}][diff]    --- ${r} before
    END
    ${r}=    Evaluate    next(iter($d.json()))
    ${cfg}=    GET On Session    portal    /api/config-history/${job}/${r}    params=which=after
    Should Not Be Empty    ${cfg.text}
    Should Not Contain    ${cfg.text}    Last configuration change at

Every customer's applications answer at every hub that serves them
    [Documentation]    The hubs carry the applications' VIPs (a CLI template on Loopback10); every customer's LAN host
    ...                pings its subscribed applications' VIPs every minute, and connects to the HTTPS ones.
    FOR    ${h}    IN    @{HUBS}
        ${lo}=    Show    ${h}    show running-config interface Loopback10
        FOR    ${a}    IN    @{APPLICATIONS}
            IF    $h in $a['vips']
                Should Contain    ${lo}    ip address ${a}[vips][${h}] 255.255.255.255 secondary
            END
        END
    END
    ${met}=    GET On Session    portal    /metrics
    FOR    ${c}    IN    @{SPOKES}
        FOR    ${aid}    IN    @{COMPANIES}[${c}][applications]
            FOR    ${h}    IN    @{APP_HUBS}[${aid}]
                Should Contain    ${met.text}    lab_app_up{lab="c8000v-dmvpn-lab",customer="${c}",app="${aid}",hub="${h}"} 1
            END
        END
    END

A customer account sees only its own service, asks for changes, and runs its own diagnostics
    ${c}=    Set Variable    ${SPOKES}[0]
    ${pw}=    Evaluate    __import__('secrets').token_urlsafe(12)
    Create Session    admin    http://127.0.0.1:8094    timeout=60
    POST On Session    admin    /api/auth/login    json=${PORTAL_LOGIN}[admin]
    DELETE On Session    admin    /api/auth/users/robot-customer    expected_status=any
    POST On Session    admin    /api/auth/users    json=${{{"username": "robot-customer", "name": "suite 07", "roles": ["customer"], "password": $pw, "customer": $c}}}
    Create Session    cust    http://127.0.0.1:8094    timeout=180
    POST On Session    cust    /api/auth/login    json=${{{"username": "robot-customer", "password": $pw}}}
    GET On Session    cust    /api/state    expected_status=403
    ${v}=    GET On Session    cust    /api/c/me/state
    Should Be Equal    ${v.json()}[customer]    ${c}
    ${d}=    POST On Session    cust    /api/c/me/diagnostics    json=${{{"kind": "apps"}}}
    FOR    ${aid}    IN    @{COMPANIES}[${c}][applications]
        Dictionary Should Contain Key    ${d.json()}[apps]    ${aid}
    END
    ${q}=    POST On Session    cust    /api/c/me/requests    json=${{{"prefer_hub": $HUBS[-1] if $PREFER.get($c) != $HUBS[-1] else $HUBS[0], "reason": "suite 07"}}}
    Should Be Equal    ${q.json()}[status]    pending
    Should Be Equal    ${q.json()}[source]    customer
    ${no}=    POST On Session    approver    /api/changes/${q.json()}[id]/reject    json=${{{"comment": "suite 07: only a test"}}}
    Should Be Equal    ${no.json()}[status]    rejected
    ${mine}=    GET On Session    cust    /api/c/me/requests
    Should Be Equal    ${mine.json()}[0][id]    ${q.json()}[id]
    Should Be Equal    ${mine.json()}[0][status]    rejected
    DELETE On Session    admin    /api/auth/users/robot-customer

The pre-shared key stays out of git and out of sight, and only an admin can rotate it
    [Documentation]    The committed renders carry a placeholder, never the key; the key lives in secrets/ on the lab
    ...                host; the portal shows it by fingerprint only and masks it in configurations it shows.
    ${sec}=    GET On Session    portal    /api/security
    Should Match Regexp    ${sec.json()}[psk][fingerprint]    ^[0-9a-f]{8}$
    ${key}=    Evaluate    __import__('subprocess').run(['python3', '-c', 'import sys; sys.path.insert(0, "tools"); import labsecrets; print(labsecrets.psk())'], capture_output=True, text=True, cwd=$LAB_ROOT).stdout.strip()
    Should Not Be Empty    ${key}
    Should Not Contain    ${sec.text}    ${key}
    FOR    ${f}    IN    nac/data/devices.nac.yaml    nodes/${VYOS_SPOKES}[0]/vyos_config.txt
        ${text}=    Read Lab File    ${f}
        Should Not Contain    ${text}    ${key}
    END
    ${vy}=    Read Lab File    nodes/${VYOS_SPOKES}[0]/vyos_config.txt
    Should Contain    ${vy}    pre-shared-secret @@DMVPN_PSK@@
    ${cfg}=    GET On Session    portal    /api/config/${HUBS}[0]
    Should Not Contain    ${cfg.json()}[output]    ${key}
    Should Contain    ${cfg.json()}[output]    pre-shared-key <secret
    POST On Session    portal    /api/runs    json=${{{"mode": "rotatepsk"}}}    expected_status=403

Capacity shows every hub's load and the room left, and the Add-a-customer plan counts it
    [Documentation]    /api/capacity reads each hub (spokes, IPsec, control-plane CPU, DRAM, WAN traffic against the
    ...                licensed throughput), the providers' customer ports and the lab host, and says how many more
    ...                customers fit and what runs out first. Only an admin changes the planning figures.
    ${cap}=    GET On Session    portal    /api/capacity
    ${c}=    Set Variable    ${cap.json()}
    Length Should Be    ${c}[hubs]    ${{len($HUBS)}}
    FOR    ${h}    IN    @{c}[hubs]
        Should Be Equal    ${h}[error]    ${None}    ${h}[name] could not be read
        ${names}=    Evaluate    [r["name"] for r in $h["resources"]]
        FOR    ${r}    IN    Spokes registered    IPsec sessions    Control-plane CPU    DRAM    WAN traffic
            Should Contain    ${names}    ${r}    ${h}[name] has no ${r} reading
        END
        Should Be True    ${h}[resources][0][used] >= 1    ${h}[name] has no spokes registered
    END
    Should Be True    ${c}[room][c8000v][customers] >= 0
    Should Not Be Empty    ${c}[room][c8000v][limited_by]
    PUT On Session    portal    /api/capacity/policy    json=${{{"hub_max_spokes": 30}}}    expected_status=403
    ${s}=    GET On Session    portal    /api/customers/suggest
    ${v}=    POST On Session    portal    /api/customers/validate    json=${s.json()}
    Should Contain    ${v.json()}[plan][capacity]    room for

*** Keywords ***
Sign In
    Create Session    portal    http://127.0.0.1:8094    timeout=180
    POST On Session    portal    /api/auth/login    json=${PORTAL_LOGIN}[operator]
    Create Session    approver    http://127.0.0.1:8094    timeout=60
    POST On Session    approver    /api/auth/login    json=${PORTAL_LOGIN}[approver]
