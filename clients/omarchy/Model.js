.pragma library

function cleanDnsName(value) {
    return String(value || "").replace(/\.$/, "")
}

function safeDnsName(value) {
    var dns = cleanDnsName(value).toLowerCase()
    return dns.length <= 253 && /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/.test(dns) ? dns : ""
}

function safePath(value) {
    var path = String(value || "")
    return ["/", "/grave", "/grave/", "/term", "/term/", "/net", "/net/"].indexOf(path) >= 0 ? path : ""
}

function candidates(status) {
    var nodes = []
    if (!status || typeof status !== "object") return nodes
    if (status.Self) nodes.push(status.Self)
    var peers = status.Peer || {}
    for (var key in peers) nodes.push(peers[key])
    var seen = {}
    return nodes.filter(function(node) {
        var dns = safeDnsName(node && node.DNSName)
        var id = String((node && (node.ID || node.StableID || node.NodeID)) || "")
        if (!node || node.Online !== true || !dns || !id || seen[id]) return false
        seen[id] = true
        return true
    }).map(function(node) { return { id: String(node.ID || node.StableID || node.NodeID), dns: safeDnsName(node.DNSName), name: String(node.HostName || safeDnsName(node.DNSName)) } })
}

function summary(raw) {
    try {
        if (String(raw || "").length > 65536) return null
        var value = JSON.parse(String(raw || ""))
        if (value.product !== "gravedecay" || value.api_version !== 1 || !value.node || !value.resources || !value.activity || !value.health || !value.links) return null
        if (["linux", "macos", "container"].indexOf(value.node.platform) < 0) return null
        var clean = {product: "gravedecay", api_version: 1,
            node: {host: String(value.node.host || "").slice(0, 256), platform: value.node.platform, mode: String(value.node.mode || "").slice(0, 64)},
            resources: {}, activity: {}, health: {}, links: {}}
        var sections = {resources: ["cpu_pct", "memory_pct", "disk_pct"], activity: ["sessions_live", "sessions_frozen"], health: ["services_failed", "containers_problem"]}
        for (var section in sections) sections[section].forEach(function(key) {
            var n = value[section][key]
            clean[section][key] = typeof n === "number" && isFinite(n) && n >= 0 && n <= 1e12 ? n : null
        })
        ;["dashboard", "t3", "terminal", "network"].forEach(function(key) { var path = safePath(value.links[key]); if (path) clean.links[key] = path })
        return clean
    } catch (_) { return null }
}

function restore(raw) {
    try {
        if (String(raw || "").length > 1048576) return []
        var saved = JSON.parse(raw), seen = {}
        if (!Array.isArray(saved.nodes)) return []
        return saved.nodes.slice(0, 128).filter(function(n) {
            return n && typeof n.id === "string" && n.id.length > 0 && n.id.length <= 256 && !seen[n.id] &&
                (seen[n.id] = true) && safeDnsName(n.dns) && typeof n.name === "string" && typeof n.lastSeen === "number" && isFinite(n.lastSeen) && summary(JSON.stringify(n.summary))
        }).map(function(n) { return {id: n.id, dns: safeDnsName(n.dns), name: n.name.slice(0, 256), lastSeen: n.lastSeen, summary: summary(JSON.stringify(n.summary)), reachable: false} })
    } catch (_) { return [] }
}

function merge(saved, found) {
    var nodes = saved.map(function(n) { return {id: n.id, dns: n.dns, name: n.name, summary: n.summary, lastSeen: n.lastSeen, reachable: false} })
    found.forEach(function(n) {
        var index = nodes.findIndex(function(old) { return old.id === n.id })
        if (index >= 0) nodes[index] = n
        else if (nodes.length < 128) nodes.push(n)
    })
    return nodes.sort(function(a, b) { return a.name.localeCompare(b.name) })
}
