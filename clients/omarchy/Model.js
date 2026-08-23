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
    return /^\/(?!\/)(?!.*[\\\x00-\x1f\x7f])/.test(path) ? path : ""
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
        var value = JSON.parse(String(raw || ""))
        if (value.product !== "gravedecay" || value.api_version !== 1 || !value.node || !value.resources || !value.activity || !value.health || !value.links) return null
        return value
    } catch (_) { return null }
}
