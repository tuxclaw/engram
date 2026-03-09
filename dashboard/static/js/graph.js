/**
 * Engram Graph Module — Reference-matching design
 *
 * Visual spec (matched to reference image):
 * - Dark #0d1117 background
 * - One central hub: bright purple glow, always-visible purple edges radiating out
 * - All other nodes: soft gray circles, varying size by degree
 * - Non-hub edges: invisible by default, appear on hover/select
 * - Labels: actual content text, white/gray, placed around nodes
 */

// ─── Colors ──────────────────────────────────────────────────────────────────
const HUB_COLOR     = "#a371f7";   // bright purple — central hub
const HUB_GLOW      = "#7c3aed";
const NODE_DEFAULT  = "#7d8590";   // soft neutral gray
const NODE_HOVER    = "#c9d1d9";   // lighter on hover/neighbor
const NODE_DIM      = "#2d333b";   // dimmed when something else is active
const EDGE_HUB      = "rgba(163, 113, 247, 0.38)"; // purple hub rays — always on
const EDGE_HOVER    = "rgba(163, 113, 247, 0.55)";  // on active node hover
const EDGE_HIDDEN   = "rgba(0,0,0,0)";              // invisible by default
const TYPE_NODE_COLORS = {
    Entity: "#7aa2f7",
    Fact: "#e0af68",
    Episode: "#bb9af7",
    Emotion: "#f7768e",
    SessionState: "#6b7280",
};

// TYPE_COLORS and TYPE_LABELS are defined in app.js (loaded after this file)

// ─── EngramGraph class ────────────────────────────────────────────────────────
class EngramGraph {
    constructor(containerId) {
        this.containerId  = containerId;
        this.container    = document.getElementById(containerId);
        this.graph        = new graphology.Graph({ multi: true, type: "directed" });
        this.renderer     = null;
        this.fa2Running   = false;
        this.hoveredNode  = null;
        this.selectedNode = null;
        this.highlightedNodes = new Set();
        this._searchHighlight = null;
        this.hiddenTypes  = new Set();
        this._initialized = false;
        this.hubNodeId    = null;   // the most-connected node

        // Callbacks
        this.onNodeClick  = null;
        this.onStageClick = null;
    }

    /** Lazy init — call when Graph tab becomes visible */
    init() {
        if (this._initialized) return;
        this._initialized = true;
        this._initRenderer();
        this._initEvents();
    }

    // ─── Renderer ─────────────────────────────────────────────────────────────
    _initRenderer() {
        this.renderer = new Sigma(this.graph, this.container, {
            // Labels
            renderLabels:              true,
            renderEdgeLabels:          false,
            labelFont:                 "'Inter', -apple-system, system-ui, sans-serif",
            labelSize:                 11,
            labelWeight:               "400",
            labelColor:                { color: "#9da7b3" },
            labelDensity:              0.07,
            labelGridCellSize:         120,
            labelRenderedSizeThreshold: 7,

            // Defaults
            defaultNodeColor:  NODE_DEFAULT,
            defaultEdgeColor:  EDGE_HIDDEN,
            defaultEdgeType:   "line",

            // Camera
            minCameraRatio: 0.008,
            maxCameraRatio: 40,
            allowInvalidContainer: true,

            // ── Edge reducer ──────────────────────────────────────────────────
            edgeReducer: (edge, data) => {
                const res = { ...data };
                const src = this.graph.source(edge);
                const tgt = this.graph.target(edge);

                // Always hide edges for hidden node types
                if (this.hiddenTypes.size > 0) {
                    const srcType = this.graph.getNodeAttribute(src, "nodeType");
                    const tgtType = this.graph.getNodeAttribute(tgt, "nodeType");
                    if (this.hiddenTypes.has(srcType) || this.hiddenTypes.has(tgtType)) {
                        res.hidden = true;
                        return res;
                    }
                }

                const active = this.selectedNode || this.hoveredNode;

                // Hub edges: always show as purple rays (unless something else is active)
                const isHubEdge = src === this.hubNodeId || tgt === this.hubNodeId;

                if (active) {
                    // Active node: show its edges bright, hub edges dimly, hide rest
                    if (src === active || tgt === active) {
                        res.color = EDGE_HOVER;
                        res.size  = 0.8;
                    } else if (isHubEdge) {
                        res.color = "rgba(163, 113, 247, 0.15)";
                        res.size  = 0.3;
                    } else {
                        res.color = EDGE_HIDDEN;
                        res.size  = 0.1;
                    }
                } else if (isHubEdge) {
                    // No active node: hub rays always visible
                    res.color = EDGE_HUB;
                    res.size  = 0.5;
                } else {
                    res.color = EDGE_HIDDEN;
                    res.size  = 0.1;
                }

                return res;
            },

            // ── Node reducer ──────────────────────────────────────────────────
            nodeReducer: (node, data) => {
                const res = { ...data };

                // Hide filtered types
                if (this.hiddenTypes.has(res.nodeType)) {
                    res.hidden = true;
                    return res;
                }

                const active = this.selectedNode || this.hoveredNode;

                if (active) {
                    if (node === active) {
                        // Active node: highlighted, slightly larger
                        res.color       = node === this.hubNodeId ? HUB_COLOR : NODE_HOVER;
                        res.highlighted = true;
                        res.zIndex      = 3;
                        res.size        = res.size * 1.6;
                    } else if (this.highlightedNodes.has(node)) {
                        // Neighbor: show with per-type color
                        res.color       = node === this.hubNodeId ? HUB_COLOR : (TYPE_NODE_COLORS[res.nodeType] || NODE_HOVER);
                        res.highlighted = true;
                        res.zIndex      = 2;
                    } else {
                        // Inactive: dim and hide label
                        res.color  = NODE_DIM;
                        res.label  = "";
                        res.zIndex = 0;
                    }
                } else {
                    // No active node: subtle per-type color, but only hub + larger nodes get labels
                    res.color  = node === this.hubNodeId ? HUB_COLOR : (TYPE_NODE_COLORS[res.nodeType] || NODE_DEFAULT);
                    res.zIndex = node === this.hubNodeId ? 2 : 1;
                    if (node !== this.hubNodeId && (res.size || 0) < 6.5) {
                        res.label = "";
                    }
                }

                // Search highlight: green
                if (this._searchHighlight && this._searchHighlight.has(node)) {
                    res.color       = "#3fb950";
                    res.highlighted = true;
                    res.zIndex      = 4;
                }

                return res;
            },
        });
    }

    // ─── Events ───────────────────────────────────────────────────────────────
    _initEvents() {
        this.renderer.on("enterNode", ({ node }) => {
            this.hoveredNode = node;
            this._updateHighlightedNodes(node);
            this.container.style.cursor = "pointer";
            this.renderer.refresh();
            this._showTooltip(node);
        });

        this.renderer.on("leaveNode", () => {
            this.hoveredNode = null;
            if (!this.selectedNode) this.highlightedNodes.clear();
            this.container.style.cursor = "default";
            this.renderer.refresh();
            this._hideTooltip();
        });

        this.renderer.on("clickNode", ({ node }) => {
            this.selectedNode = node;
            this._updateHighlightedNodes(node);
            this.renderer.refresh();
            if (this.onNodeClick) this.onNodeClick(node);
        });

        this.renderer.on("clickStage", () => {
            this.selectedNode = null;
            this.highlightedNodes.clear();
            this.renderer.refresh();
            if (this.onStageClick) this.onStageClick();
        });
    }

    _updateHighlightedNodes(node) {
        this.highlightedNodes.clear();
        this.highlightedNodes.add(node);
        this.graph.forEachNeighbor(node, (n) => this.highlightedNodes.add(n));
    }

    // ─── Tooltip ──────────────────────────────────────────────────────────────
    _showTooltip(node) {
        this._hideTooltip();
        const data = this.graph.getNodeAttributes(node);
        const pos  = this.renderer.getNodeDisplayData(node);
        if (!pos) return;

        const tip = document.createElement("div");
        tip.className = "sigma-tooltip";
        tip.id        = "active-tooltip";
        const typeLabel = data.nodeType || "Node";
        tip.innerHTML = `
            <div class="tt-label">${escapeHtml(data.fullLabel || data.label || node)}</div>
            <div class="tt-meta">${typeLabel} · ${(data.degree || 0).toLocaleString()} connections</div>
        `;
        tip.style.left = (pos.x + 16) + "px";
        tip.style.top  = (pos.y + 16) + "px";
        this.container.appendChild(tip);
    }

    _hideTooltip() {
        document.getElementById("active-tooltip")?.remove();
    }

    // ─── Load data ────────────────────────────────────────────────────────────
    loadData(data, forceHubId = null) {
        const { nodes, edges } = data;

        // Identify hub: use forceHubId if provided, else highest-degree node
        if (forceHubId) {
            this.hubNodeId = forceHubId;
        } else if (!this.hubNodeId) {
            let maxDeg = 0;
            nodes.forEach((n) => { if ((n.degree || 0) > maxDeg) { maxDeg = n.degree; this.hubNodeId = n.id; } });
        }

        nodes.forEach((n) => {
            if (this.graph.hasNode(n.id)) return;

            const isHub = n.id === this.hubNodeId;
            const deg   = n.degree || 0;

            // Size: hub is big, others scale gently with degree
            const size = isHub
                ? Math.max(16, Math.min(24, 16 + Math.log2(deg + 1) * 0.8))
                : Math.max(2.5,  Math.min(8.5, 2.5  + Math.log2(deg + 1) * 0.45));

            // Position: hub at center, others spread wide
            const x = isHub ? 0 : (Math.random() - 0.5) * 6000;
            const y = isHub ? 0 : (Math.random() - 0.5) * 6000;

            this.graph.addNode(n.id, {
                label:    this._smartLabel(n.label, isHub ? 50 : 45),
                fullLabel: n.label,
                x, y, size,
                color:    isHub ? HUB_COLOR : NODE_DEFAULT,
                nodeType: n.type,
                degree:   deg,
                created_at: n.created_at,
                isHub,
                zIndex:   isHub ? 10 : 1,
            });
        });

        edges.forEach((e, i) => {
            const key = `${e.source}-${e.type}-${e.target}-${i}`;
            if (this.graph.hasEdge(key)) return;
            if (!this.graph.hasNode(e.source) || !this.graph.hasNode(e.target)) return;
            try {
                this.graph.addEdgeWithKey(key, e.source, e.target, {
                    color:   EDGE_HIDDEN,
                    size:    0.1,
                    relType: e.type,
                });
            } catch (_) { /* duplicate */ }
        });

        this._updateStats();
    }

    /** Produce a label that reads like actual content, not a truncated hash */
    _smartLabel(label, maxLen) {
        if (!label) return "?";
        // Strip common prefixes that aren't human-readable
        const clean = label.replace(/^(session_|node_|fact_|ep_)/i, "");
        return clean.length > maxLen ? clean.slice(0, maxLen) + "…" : clean;
    }

    _updateStats() {
        const el  = document.getElementById("stat-visible");
        if (el)  el.textContent  = this.graph.order.toLocaleString();
        const el2 = document.getElementById("stat-edges");
        if (el2) el2.textContent = this.graph.size.toLocaleString();
    }

    // ─── Layout ───────────────────────────────────────────────────────────────
    /**
     * ForceAtlas2 tuned for star-of-stars topology:
     * - Strong repulsion keeps nodes spread
     * - Low gravity so hub stays central but everything else breathes
     * - linLogMode gives gentle attraction, prevents clumping
     */
    runLayout(iterations = 1000) {
        if (this.fa2Running) return;
        this.fa2Running = true;

        const n = this.graph.order;

        ForceAtlas2.assign(this.graph, {
            iterations,
            gravity:                        0.05,
            scalingRatio:                   120,
            slowDown:                       Math.max(2, Math.log(n) * 1.5),
            barnesHutOptimize:              n > 150,
            barnesHutTheta:                 0.6,
            strongGravityMode:              true,
            outboundAttractionDistribution: true,
            adjustSizes:                    true,
            linLogMode:                     true,
            edgeWeightInfluence:            0,
        });
        this.fitToView();

        this.fa2Running = false;
        if (this.renderer) this.renderer.refresh();
    }

    // ─── Camera ───────────────────────────────────────────────────────────────
    focusNode(nodeId) {
        if (!this.graph.hasNode(nodeId)) return;
        const attrs = this.graph.getNodeAttributes(nodeId);

        // Find graph bounding box to normalize position into camera space
        let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
        this.graph.forEachNode((_, a) => {
            minX = Math.min(minX, a.x); maxX = Math.max(maxX, a.x);
            minY = Math.min(minY, a.y); maxY = Math.max(maxY, a.y);
        });
        const rangeX = Math.max(1, maxX - minX);
        const rangeY = Math.max(1, maxY - minY);

        // Normalize node position to [0, 1] space (matching Sigma camera coords)
        const nx = (attrs.x - minX) / rangeX;
        const ny = (attrs.y - minY) / rangeY;

        this.renderer.getCamera().animate({ x: nx, y: ny, ratio: 0.35 }, { duration: 500 });

        this.selectedNode = nodeId;
        this._updateHighlightedNodes(nodeId);
        this.renderer.refresh();
    }

    zoomIn()    { const c = this.renderer.getCamera(); c.animate({ ratio: c.ratio / 1.5 }, { duration: 200 }); }
    zoomOut()   { const c = this.renderer.getCamera(); c.animate({ ratio: c.ratio * 1.5 }, { duration: 200 }); }
    zoomReset() { this.fitToView(); }

    fitToView() {
        if (!this.renderer || this.graph.order === 0) return;
        // Sigma v2 normalizes graph coordinates internally.
        // Camera (0.5, 0.5, ratio=1) = centered, showing the full graph.
        this.renderer.getCamera().animate({ x: 0.5, y: 0.5, ratio: 1 }, { duration: 400 });
    }

    // ─── Type filtering ───────────────────────────────────────────────────────
    toggleType(type) {
        this.hiddenTypes.has(type) ? this.hiddenTypes.delete(type) : this.hiddenTypes.add(type);
        if (this.renderer) this.renderer.refresh();
    }

    // ─── Search ───────────────────────────────────────────────────────────────
    highlightSearch(nodeIds) {
        this._searchHighlight = new Set(nodeIds);
        if (this.renderer) this.renderer.refresh();
        setTimeout(() => { this._searchHighlight = null; if (this.renderer) this.renderer.refresh(); }, 6000);
    }

    clearSearch() {
        this._searchHighlight = null;
        if (this.renderer) this.renderer.refresh();
    }
}

// ─── Shared utility ───────────────────────────────────────────────────────────
function escapeHtml(str) {
    if (!str) return "";
    const d = document.createElement("div");
    d.textContent = str;
    return d.innerHTML;
}
