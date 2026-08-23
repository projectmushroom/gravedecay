.pragma library

function cleanDnsName(value) {
    return String(value || "").replace(/\.$/, "")
}

function candidates(status) {
    var nodes = []
    if (!status || typeof status !== "object") return nodes
    if (status.Self) nodes.push(status.Self)
    var peers = status.Peer || {}
    for (var key in peers) nodes.push(peers[key])
    var seen = {}
    return nodes.filter(function(node) {
        var dns = cleanDnsName(node && node.DNSName)
        var id = String((node && (node.ID || node.StableID || node.NodeID)) || "")
        if (!node || node.Online !== true || !dns || !id || seen[id]) return false
        seen[id] = true
        return true
    }).map(function(node) { return { id: String(node.ID || node.StableID || node.NodeID), dns: cleanDnsName(node.DNSName), name: String(node.HostName || cleanDnsName(node.DNSName)) } })
}

function summary(raw) {
    try {
        var value = JSON.parse(String(raw || ""))
        if (value.product !== "gravedecay" || value.api_version !== 1 || !value.node || !value.resources || !value.activity || !value.health || !value.links) return null
        return value
    } catch (_) { return null }
}
