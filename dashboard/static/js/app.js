/**
 * Engram Dashboard — Main Application
 * Handles tabs, overview, browse, search, and graph integration.
 */

const API_BASE = window.location.origin;

// Sidebar/UI colors (separate from graph colors which are monochrome)
const TYPE_COLORS = {
    Entity:       "#58a6ff",
    Fact:         "#e3b341",
    Episode:      "#bc8cff",
    Emotion:      "#f778ba",
    SessionState: "#6e7681",
};

const TYPE_LABELS = {
    Entity:       "Entity",
    Fact:         "Memory",
    Episode:      "Episode",
    Emotion:      "Emotion",
    SessionState: "Session",
};

const TYPE_ICONS = {
    Entity:       "fa-user-circle",
    Fact:         "fa-brain",
    Episode:      "fa-bookmark",
    Emotion:      "fa-heart",
    SessionState: "fa-terminal",
};

let engramGraph;
let currentOffset = 0;
let currentLimit = 500;
let totalNodes = 0;
let searchTimeout = null;
let graphInitialized = false;
let graphDataLoaded = false;

// Browse state
let browseData = [];
let browseOffset = 0;
let browseLimit = 50;
let browseTotal = 0;
let browseSearchTimeout = null;

// Agent filter state
let currentAgentFilter = "";  // empty = all agents

// ============================================================
// Initialization
// ============================================================

document.addEventListener("DOMContentLoaded", async () => {
    engramGraph = new EngramGraph("graph-container");

    // Wire up tab navigation
    initTabs();

    // Wire up search
    initSearch();

    // Wire up agent filter
    initAgentFilter();

    // Wire up explore button
    document.getElementById("btn-explore-graph").addEventListener("click", () => {
        switchTab("graph");
    });

    // Check DB availability first
    hideLoading();
    await checkDbAndLoad();
});

// ============================================================
// Tab Management
// ============================================================

// Agent filter helper — appends agent_id param to URL if set
function agentParam(prefix = "&") {
    return currentAgentFilter ? `${prefix}agent_id=${encodeURIComponent(currentAgentFilter)}` : "";
}

async function initAgentFilter() {
    const select = document.getElementById("agent-filter");
    if (!select) return;
    
    try {
        const resp = await fetch(`${API_BASE}/api/agents`);
        const data = await resp.json();
        
        // Add agent options
        const agentNames = { main: "💠 Jarvis", loopfans: "🔁 LoopAgent", sillyfarms: "🌿 SillyFarms", shared: "🌐 Shared" };
        for (const agent of data.agents || []) {
            const opt = document.createElement("option");
            opt.value = agent.id;
            opt.textContent = `${agentNames[agent.id] || agent.id} (${agent.facts.toLocaleString()})`;
            select.appendChild(opt);
        }
    } catch (e) {
        console.error("Failed to load agents:", e);
    }
    
    select.addEventListener("change", () => {
        currentAgentFilter = select.value;
        // Reload current tab with new filter
        const activeTab = document.querySelector(".tab-btn.active")?.dataset.tab || "overview";
        if (activeTab === "overview") loadOverview();
        else if (activeTab === "graph") reloadGraph();
        else if (activeTab === "browse") { browseData = []; loadBrowseData(); }
    });
}

function initTabs() {
    const tabBtns = document.querySelectorAll(".tab-btn");
    tabBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            switchTab(btn.dataset.tab);
        });
    });
}

function switchTab(tabName) {
    // Update buttons
    document.querySelectorAll(".tab-btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.tab === tabName);
    });

    // Update content
    document.querySelectorAll(".tab-content").forEach(content => {
        const isActive = content.id === `tab-${tabName}`;
        content.classList.toggle("active", isActive);
    });

    // Lazy load tab content
    if (tabName === "graph") {
        if (!graphInitialized) {
            // Delay init slightly so the container has dimensions
            setTimeout(() => initGraphTab(), 50);
        } else if (engramGraph && engramGraph.renderer) {
            // Tab became visible again — tell Sigma to resize
            setTimeout(() => {
                engramGraph.renderer.resize();
                engramGraph.renderer.refresh();
            }, 50);
        }
    }

    if (tabName === "browse" && browseData.length === 0) {
        loadBrowseData();
    }
}

// ============================================================
// Overview Tab
// ============================================================

async function checkDbAndLoad() {
    try {
        const resp = await fetch(`${API_BASE}/api/status`);
        if (resp.status === 503) {
            // DB locked — memory indexing in progress
            showIndexingBanner();
            // Retry every 15 seconds
            setTimeout(checkDbAndLoad, 15000);
            return;
        }
        hideIndexingBanner();
        await loadOverview();
    } catch (err) {
        console.error("Status check failed:", err);
        updateConnectionStatus(false);
    }
}

function showIndexingBanner() {
    let banner = document.getElementById("indexing-banner");
    if (!banner) {
        banner = document.createElement("div");
        banner.id = "indexing-banner";
        banner.style.cssText = `
            position: fixed; top: 58px; left: 50%; transform: translateX(-50%);
            background: rgba(163,113,247,0.15); border: 1px solid rgba(163,113,247,0.3);
            color: #bc8cff; padding: 8px 20px; border-radius: 20px; font-size: 13px;
            z-index: 2000; display: flex; align-items: center; gap: 8px;
        `;
        banner.innerHTML = `<span style="animation: spin 1s linear infinite; display:inline-block">⟳</span> Memory indexing in progress — reconnecting automatically…`;
        document.body.appendChild(banner);
    }
    updateConnectionStatus(false);
}

function hideIndexingBanner() {
    document.getElementById("indexing-banner")?.remove();
    updateConnectionStatus(true);
}

async function loadOverview() {
    try {
        // Load stats, recent memories, top entities, and timeline in parallel
        const [statsResp, recentResp, entitiesResp, timelineResp] = await Promise.allSettled([
            fetch(`${API_BASE}/api/stats${agentParam('?')}`),
            fetch(`${API_BASE}/api/graph?limit=10${agentParam()}`),
            fetch(`${API_BASE}/api/graph?node_type=Entity&limit=10${agentParam()}`),
            fetch(`${API_BASE}/api/timeline`),
        ]);

        // Stats
        if (statsResp.status === "fulfilled") {
            const stats = await statsResp.value.json();
            animateCounter("facts", stats.nodes.Fact || 0);
            animateCounter("entities", stats.nodes.Entity || 0);
            animateCounter("episodes", stats.nodes.Episode || 0);
            animateCounter("emotions", stats.nodes.Emotion || 0);
            animateCounter("relationships", stats.total_rels || 0);
            updateConnectionStatus(true);
        } else {
            updateConnectionStatus(false);
        }

        // Recent memories
        if (recentResp.status === "fulfilled") {
            const data = await recentResp.value.json();
            renderRecentMemories(data.nodes || []);
        }

        // Top entities
        if (entitiesResp.status === "fulfilled") {
            const data = await entitiesResp.value.json();
            renderTopEntities(data.nodes || []);
        }

        // Timeline
        if (timelineResp.status === "fulfilled") {
            const data = await timelineResp.value.json();
            renderTimeline(data.timeline || []);
        }

    } catch (err) {
        console.error("Failed to load overview:", err);
        updateConnectionStatus(false);
    }
}

function animateCounter(key, target) {
    const el = document.querySelector(`[data-counter="${key}"]`);
    if (!el) return;

    const duration = 1200;
    const startTime = performance.now();
    const start = 0;

    function update(currentTime) {
        const elapsed = currentTime - startTime;
        const progress = Math.min(elapsed / duration, 1);

        // Ease out cubic
        const eased = 1 - Math.pow(1 - progress, 3);
        const current = Math.round(start + (target - start) * eased);

        el.textContent = current.toLocaleString();

        if (progress < 1) {
            requestAnimationFrame(update);
        }
    }

    requestAnimationFrame(update);
}

function renderRecentMemories(nodes) {
    const container = document.getElementById("recent-memories");

    // Sort by created_at descending
    const sorted = [...nodes].sort((a, b) => {
        if (!a.created_at) return 1;
        if (!b.created_at) return -1;
        return new Date(b.created_at) - new Date(a.created_at);
    });

    if (sorted.length === 0) {
        container.innerHTML = '<div class="loading-placeholder">No memories yet</div>';
        return;
    }

    container.innerHTML = sorted.map(node => {
        const color = TYPE_COLORS[node.type] || "#6e7681";
        const bgClass = getBgClass(node.type);
        const icon = TYPE_ICONS[node.type] || "fa-circle";
        const typeLabel = TYPE_LABELS[node.type] || node.type;
        const dateStr = formatDate(node.created_at);

        return `
            <div class="memory-item" data-node-id="${escapeAttr(node.id)}">
                <div class="memory-item__icon ${bgClass}">
                    <i class="fas ${icon}"></i>
                </div>
                <div class="memory-item__body">
                    <div class="memory-item__text">${escapeHtml(node.label)}</div>
                    <div class="memory-item__meta">
                        <span class="type-pill ${bgClass}">${typeLabel}</span>
                        ${dateStr ? `<span>${dateStr}</span>` : ''}
                        ${node.degree ? `<span><i class="fas fa-link" style="font-size:9px"></i> ${node.degree}</span>` : ''}
                    </div>
                </div>
            </div>
        `;
    }).join('');

    // Click handlers
    container.querySelectorAll(".memory-item").forEach(item => {
        item.addEventListener("click", () => {
            const nodeId = item.dataset.nodeId;
            switchTab("graph");
            setTimeout(() => loadNodeIntoGraph(nodeId), 100);
        });
    });
}

function renderTopEntities(nodes) {
    const container = document.getElementById("top-entities");

    if (nodes.length === 0) {
        container.innerHTML = '<div class="loading-placeholder">No entities yet</div>';
        return;
    }

    container.innerHTML = nodes.map(node => `
        <div class="entity-item" data-node-id="${escapeAttr(node.id)}">
            <div class="entity-item__dot"></div>
            <div class="entity-item__name">${escapeHtml(node.label)}</div>
            <div class="entity-item__connections">
                <i class="fas fa-link"></i>
                ${node.degree || 0}
            </div>
        </div>
    `).join('');

    container.querySelectorAll(".entity-item").forEach(item => {
        item.addEventListener("click", () => {
            const nodeId = item.dataset.nodeId;
            switchTab("graph");
            setTimeout(() => loadNodeIntoGraph(nodeId), 100);
        });
    });
}

function renderTimeline(timeline) {
    const container = document.getElementById("memory-timeline");

    if (!timeline || timeline.length === 0) {
        container.innerHTML = '<div class="loading-placeholder">No timeline data yet</div>';
        return;
    }

    // Get last 30 days of data
    const recent = timeline.slice(-30);

    // Find max total for scaling
    let maxTotal = 0;
    recent.forEach(day => {
        let total = 0;
        Object.keys(day).forEach(key => {
            if (key !== 'date') total += (day[key] || 0);
        });
        if (total > maxTotal) maxTotal = total;
    });

    if (maxTotal === 0) maxTotal = 1;
    const maxHeight = 120;

    const types = ['Entity', 'Fact', 'Episode', 'Emotion', 'SessionState'];

    container.innerHTML = `
        <div class="timeline-bars">
            ${recent.map(day => {
                let total = 0;
                types.forEach(t => total += (day[t] || 0));

                const tooltipParts = types
                    .filter(t => day[t])
                    .map(t => `${TYPE_LABELS[t] || t}: ${day[t]}`)
                    .join(' · ');

                const dateLabel = day.date ? new Date(day.date + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : '';

                return `
                    <div class="timeline-bar-group">
                        <div class="timeline-tooltip">${dateLabel}: ${total} total<br>${tooltipParts}</div>
                        <div class="timeline-bar-stack" style="height:${maxHeight}px">
                            ${types.map(t => {
                                const count = day[t] || 0;
                                if (count === 0) return '';
                                const h = Math.max(2, (count / maxTotal) * maxHeight);
                                return `<div class="timeline-bar" style="height:${h}px;background:${TYPE_COLORS[t]}"></div>`;
                            }).join('')}
                        </div>
                        <span class="timeline-date">${dateLabel}</span>
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

// ============================================================
// Graph Tab
// ============================================================

async function initGraphTab() {
    graphInitialized = true;

    // Initialize renderer
    engramGraph.init();
    engramGraph.onNodeClick = handleNodeClick;
    engramGraph.onStageClick = handleStageClick;

    // Wire up controls
    document.getElementById("btn-reload").addEventListener("click", reloadGraph);
    document.getElementById("btn-layout").addEventListener("click", () => engramGraph.runLayout(500));
    document.getElementById("btn-expand").addEventListener("click", loadMore);
    document.getElementById("filter-type").addEventListener("change", reloadGraph);
    document.getElementById("sidebar-close").addEventListener("click", closeSidebar);

    // Zoom controls
    document.getElementById("btn-zoom-in").addEventListener("click", () => engramGraph.zoomIn());
    document.getElementById("btn-zoom-out").addEventListener("click", () => engramGraph.zoomOut());
    document.getElementById("btn-zoom-reset").addEventListener("click", () => engramGraph.zoomReset());

    // Legend toggle
    document.querySelectorAll(".legend-item").forEach(item => {
        item.addEventListener("click", () => {
            const type = item.dataset.type;
            item.classList.toggle("active");
            engramGraph.toggleType(type);
        });
    });

    // Load graph data
    showLoading();
    await loadGraph();
    setTimeout(() => {
        engramGraph.runLayout(500);
        engramGraph.fitToView();
        hideLoading();
    }, 300);
}

async function loadGraph(append = false) {
    try {
        const filter = document.getElementById("filter-type").value;
        const params = new URLSearchParams({
            limit: currentLimit,
            offset: append ? currentOffset : 0,
        });
        if (filter) params.set("node_type", filter);

        const resp = await fetch(`${API_BASE}/api/graph?${params}${agentParam()}`);
        if (!resp.ok) {
            if (resp.status === 500 || resp.status === 503) {
                showIndexingBanner();
                setTimeout(() => initGraphTab(), 15000);
                return;
            }
            throw new Error(`HTTP ${resp.status}`);
        }
        hideIndexingBanner();
        const data = await resp.json();

        totalNodes = data.total;
        if (!append) currentOffset = 0;
        currentOffset += data.nodes.length;

        engramGraph.loadData(data);
        graphDataLoaded = true;
    } catch (err) {
        console.error("Failed to load graph:", err);
    }
}

async function reloadGraph() {
    showLoading();
    engramGraph.graph.clear();
    currentOffset = 0;
    await loadGraph();
    setTimeout(() => {
        engramGraph.runLayout(500);
        engramGraph.fitToView();
        hideLoading();
    }, 300);
}

async function loadMore() {
    if (currentOffset >= totalNodes) return;
    showLoading();
    await loadGraph(true);
    setTimeout(() => {
        engramGraph.runLayout(250);
        engramGraph.fitToView();
        hideLoading();
    }, 200);
}

// ============================================================
// Browse Tab
// ============================================================

async function loadBrowseData() {
    const typeFilter = document.getElementById("browse-type-filter").value;
    const sortBy = document.getElementById("browse-sort").value;
    const searchQuery = document.getElementById("browse-search-input").value.trim();

    try {
        let url;
        if (searchQuery.length >= 2) {
            url = `${API_BASE}/api/search?q=${encodeURIComponent(searchQuery)}&limit=200${agentParam()}`;
        } else {
            const params = new URLSearchParams({ limit: 200 });
            if (typeFilter) params.set("node_type", typeFilter);
            url = `${API_BASE}/api/graph?${params}${agentParam()}`;
        }

        const resp = await fetch(url);
        const data = await resp.json();

        // Normalize data shape
        browseData = searchQuery.length >= 2 ? (data.results || []) : (data.nodes || []);
        browseTotal = browseData.length;

        // Apply type filter on search results
        if (typeFilter && searchQuery.length >= 2) {
            browseData = browseData.filter(n => n.type === typeFilter);
        }

        // Sort
        sortBrowseData(sortBy);

        // Render
        browseOffset = 0;
        renderBrowseGrid(false);

    } catch (err) {
        console.error("Failed to load browse data:", err);
    }
}

function sortBrowseData(sortBy) {
    switch (sortBy) {
        case "connections":
            browseData.sort((a, b) => (b.degree || 0) - (a.degree || 0));
            break;
        case "recent":
            browseData.sort((a, b) => {
                if (!a.created_at) return 1;
                if (!b.created_at) return -1;
                return new Date(b.created_at) - new Date(a.created_at);
            });
            break;
        case "alpha":
            browseData.sort((a, b) => (a.label || '').localeCompare(b.label || ''));
            break;
    }
}

function renderBrowseGrid(append) {
    const container = document.getElementById("browse-grid");
    const loadMoreBtn = document.getElementById("btn-browse-more");

    const pageSize = 30;
    const start = append ? browseOffset : 0;
    const end = Math.min(start + pageSize, browseData.length);
    browseOffset = end;

    if (!append) container.innerHTML = '';

    if (browseData.length === 0) {
        container.innerHTML = '<div class="loading-placeholder">No memories found</div>';
        loadMoreBtn.style.display = 'none';
        return;
    }

    const fragment = document.createDocumentFragment();

    for (let i = start; i < end; i++) {
        const node = browseData[i];
        const color = TYPE_COLORS[node.type] || "#6e7681";
        const bgClass = getBgClass(node.type);
        const icon = TYPE_ICONS[node.type] || "fa-circle";
        const typeLabel = TYPE_LABELS[node.type] || node.type;
        const dateStr = formatDate(node.created_at);

        const card = document.createElement("div");
        card.className = "browse-card";
        card.dataset.nodeId = node.id;
        card.innerHTML = `
            <div class="browse-card__header">
                <div class="browse-card__icon ${bgClass}">
                    <i class="fas ${icon}"></i>
                </div>
                <div class="browse-card__title">
                    <div class="browse-card__name">${escapeHtml(node.label)}</div>
                    <span class="browse-card__type ${bgClass}">${typeLabel}</span>
                </div>
            </div>
            <div class="browse-card__footer">
                <span>${dateStr || 'No date'}</span>
                <span class="browse-card__connections">
                    <i class="fas fa-link"></i>
                    ${node.degree || 0} connections
                </span>
            </div>
        `;

        card.addEventListener("click", () => {
            switchTab("graph");
            setTimeout(() => loadNodeIntoGraph(node.id), 100);
        });

        fragment.appendChild(card);
    }

    container.appendChild(fragment);

    // Show/hide load more
    loadMoreBtn.style.display = end < browseData.length ? '' : 'none';
}

// Wire up browse controls
document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("browse-type-filter").addEventListener("change", () => loadBrowseData());
    document.getElementById("browse-sort").addEventListener("change", () => {
        const sortBy = document.getElementById("browse-sort").value;
        sortBrowseData(sortBy);
        browseOffset = 0;
        renderBrowseGrid(false);
    });

    document.getElementById("browse-search-input").addEventListener("input", () => {
        clearTimeout(browseSearchTimeout);
        browseSearchTimeout = setTimeout(() => loadBrowseData(), 400);
    });

    document.getElementById("btn-browse-more").addEventListener("click", () => {
        renderBrowseGrid(true);
    });
});

// ============================================================
// Search (Global)
// ============================================================

function initSearch() {
    const searchInput = document.getElementById("search-input");

    searchInput.addEventListener("input", (e) => {
        clearTimeout(searchTimeout);
        const q = e.target.value.trim();
        if (q.length < 2) {
            hideSearchResults();
            return;
        }
        searchTimeout = setTimeout(() => doSearch(q), 300);
    });

    searchInput.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            searchInput.value = "";
            hideSearchResults();
            engramGraph.clearSearch();
        }
    });

    // Click outside to close
    document.addEventListener("click", (e) => {
        if (!e.target.closest(".search-container")) {
            hideSearchResults();
        }
    });
}

async function doSearch(query) {
    try {
        const resp = await fetch(`${API_BASE}/api/search?q=${encodeURIComponent(query)}&limit=20${agentParam()}`);
        const data = await resp.json();
        showSearchResults(data.results);
    } catch (err) {
        console.error("Search failed:", err);
    }
}

function showSearchResults(results) {
    const dropdown = document.getElementById("search-results");
    dropdown.innerHTML = "";

    if (results.length === 0) {
        dropdown.innerHTML = '<div class="search-item"><span class="label" style="color:var(--text-muted)">No results found</span></div>';
        dropdown.classList.add("visible");
        return;
    }

    results.forEach((r) => {
        const color = TYPE_COLORS[r.type] || "#6e7681";
        const div = document.createElement("div");
        div.className = "search-item";
        div.innerHTML = `
            <span class="type-dot" style="background:${color}"></span>
            <span class="label">${escapeHtml(r.label)}</span>
            <span class="degree">${r.degree} conn</span>
        `;
        div.addEventListener("click", () => {
            switchTab("graph");
            setTimeout(() => {
                if (engramGraph.graph.hasNode(r.id)) {
                    engramGraph.focusNode(r.id);
                    handleNodeClick(r.id);
                } else {
                    loadNodeIntoGraph(r.id);
                }
            }, 100);
            hideSearchResults();
            document.getElementById("search-input").value = "";
        });
        dropdown.appendChild(div);
    });

    dropdown.classList.add("visible");

    // Highlight in graph if visible
    if (graphDataLoaded) {
        const ids = results.map(r => r.id).filter(id => engramGraph.graph.hasNode(id));
        if (ids.length > 0) {
            engramGraph.highlightSearch(ids);
        }
    }
}

function hideSearchResults() {
    document.getElementById("search-results").classList.remove("visible");
}

async function loadNodeIntoGraph(nodeId) {
    // Ensure graph tab is initialized
    if (!graphInitialized) {
        await initGraphTab();
    }

    showLoading();
    try {
        // Fetch the node's own details + all neighbors in parallel
        const [neighborsResp, detailResp] = await Promise.all([
            fetch(`${API_BASE}/api/graph/neighbors/${nodeId}`),
            fetch(`${API_BASE}/api/node/${nodeId}`),
        ]);

        const data = await neighborsResp.json();
        const detail = await detailResp.json();

        // Build the focal node
        const focalNode = {
            id: nodeId,
            label: detail.name || detail.label || detail.content || detail.summary || nodeId,
            type: detail._type || "Entity",
            degree: detail._degree || data.neighbors.length,
            created_at: detail.created_at,
        };

        // Neighbors
        const nodes = [
            focalNode,
            ...data.neighbors.filter(n => n.id !== nodeId),
        ];

        // CLEAR the existing graph and rebuild focused on this node
        engramGraph.graph.clear();
        engramGraph.hubNodeId = null; // Reset so loadData picks up forceHubId
        graphDataLoaded = false;

        // Load data, forcing the focal node as the hub
        engramGraph.loadData({ nodes, edges: data.edges }, nodeId);

        // Force renderer to acknowledge the new graph state
        if (engramGraph.renderer) {
            engramGraph.renderer.refresh();
        }

        // Force Sigma to acknowledge the cleared/reloaded graph
        if (engramGraph.renderer) {
            engramGraph.renderer.resize();
            engramGraph.renderer.refresh();
        }

        // Run layout, center, then open sidebar
        setTimeout(() => {
            engramGraph.runLayout(600);
            if (engramGraph.renderer) {
                engramGraph.renderer.refresh();
            }
            // Animate camera to center after a tick so Sigma recalculates extents
            requestAnimationFrame(() => {
                engramGraph.fitToView();
                if (engramGraph.renderer) engramGraph.renderer.refresh();
            });
            hideLoading();
            // Open the sidebar for this node
            handleNodeClick(nodeId);
        }, 300);
    } catch (err) {
        console.error("Failed to load node:", err);
        hideLoading();
    }
}

// ============================================================
// Node Detail Sidebar
// ============================================================

async function handleNodeClick(nodeId) {
    const sidebar = document.getElementById("sidebar");
    sidebar.classList.remove("hidden");
    sidebar.classList.add("visible");

    // Show ego-graph for selected node (non-blocking) — but not if we just loaded it
    if (engramGraph.hubNodeId !== nodeId && engramGraph.graph.hasNode(nodeId) && engramGraph.graph.order > 1) {
        // Only rebuild ego-graph if clicking a different node in a populated graph
        // Don't rebuild if we just loaded from loadNodeIntoGraph
    }

    const attrs = engramGraph.graph.hasNode(nodeId)
        ? engramGraph.graph.getNodeAttributes(nodeId)
        : {};

    // Set title
    document.getElementById("sidebar-title").textContent = attrs.fullLabel || attrs.label || nodeId;

    // Badge
    const badge = document.getElementById("sidebar-type");
    const typeLabel = TYPE_LABELS[attrs.nodeType] || attrs.nodeType || "Node";
    badge.textContent = typeLabel;
    badge.className = "sidebar-badge " + getBgClass(attrs.nodeType);

    // Clear description
    const descEl = document.getElementById("sidebar-description");
    descEl.textContent = "";

    // Fetch full details
    try {
        const resp = await fetch(`${API_BASE}/api/node/${nodeId}`);
        const detail = await resp.json();

        // Pull out description/content as the hero text
        const heroText = detail.description || detail.content || detail.summary || "";
        if (heroText && heroText !== "None") {
            descEl.textContent = heroText.length > 300 ? heroText.slice(0, 300) + "…" : heroText;
        }

        const detailsEl = document.getElementById("sidebar-details");
        detailsEl.innerHTML = "";

        // Keys to skip entirely (already shown elsewhere or internal)
        const skipKeys = new Set([
            "_type", "_degree", "_id", "_label", "_offset",
            "id", "name", "description", "content", "summary",
            "metadata", "agent_id"
        ]);

        const keyLabels = {
            entity_type:  "Type",
            category:     "Category",
            source:       "Source",
            label:        "Label",
            session_key:  "Session",
            created_at:   "Created",
            updated_at:   "Updated",
            last_accessed:"Last seen",
            confidence:   "Confidence",
            importance:   "Importance",
            valence:      "Valence",
            intensity:    "Intensity",
            access_count: "Views",
        };

        // Priority key order
        const keyOrder = ["entity_type", "category", "label", "valence", "intensity",
                          "confidence", "importance", "access_count", "created_at", "updated_at", "last_accessed"];
        const orderedEntries = [
            ...keyOrder.filter(k => detail[k] !== undefined),
            ...Object.keys(detail).filter(k => !keyOrder.includes(k)),
        ];

        for (const key of orderedEntries) {
            const val = detail[key];
            if (skipKeys.has(key) || val === null || val === undefined || val === "None" || val === "") continue;

            const row = document.createElement("div");
            row.className = "detail-row";

            let displayVal = val;
            if (typeof val === "string" && val.length > 250) {
                displayVal = val.slice(0, 250) + "…";
            }
            if (key.includes("_at") || key === "last_accessed") {
                displayVal = formatDate(String(val)) || val;
            }
            if (key === "importance" || key === "confidence") {
                const num = parseFloat(val);
                if (!isNaN(num)) displayVal = (num * 100).toFixed(0) + "%";
            }

            const displayKey = keyLabels[key] || key.replace(/_/g, ' ');

            row.innerHTML = `
                <span class="detail-key">${escapeHtml(displayKey)}</span>
                <span class="detail-value">${escapeHtml(String(displayVal))}</span>
            `;
            detailsEl.appendChild(row);
        }
    } catch (err) {
        console.error("Failed to load node detail:", err);
    }

    // Load connections
    try {
        const resp = await fetch(`${API_BASE}/api/graph/neighbors/${nodeId}`);
        const data = await resp.json();

        document.getElementById("sidebar-conn-count").textContent = `(${data.neighbors.length})`;

        const connEl = document.getElementById("sidebar-connections");
        connEl.innerHTML = "";

        // Group by relationship type
        const byRel = {};
        data.edges.forEach(e => {
            const key = e.type;
            if (!byRel[key]) byRel[key] = [];
            const neighborId = e.source === nodeId ? e.target : e.source;
            const neighbor = data.neighbors.find(n => n.id === neighborId);
            if (neighbor && !byRel[key].find(existing => existing.id === neighbor.id)) {
                byRel[key].push(neighbor);
            }
        });

        for (const [relType, neighbors] of Object.entries(byRel)) {
            // Collapsible header
            const header = document.createElement("div");
            header.className = "conn-group-header";
            header.innerHTML = `<i class="fas fa-chevron-down"></i> ${relType.replace(/_/g, ' ')} (${neighbors.length})`;

            const group = document.createElement("div");
            group.className = "conn-group";

            header.addEventListener("click", () => {
                header.classList.toggle("collapsed");
                group.classList.toggle("collapsed");
            });

            neighbors.slice(0, 30).forEach(n => {
                const color = TYPE_COLORS[n.type] || "#6e7681";
                const typeLabel = TYPE_LABELS[n.type] || n.type;

                const div = document.createElement("div");
                div.className = "conn-item";
                div.innerHTML = `
                    <span class="conn-dot" style="background:${color}"></span>
                    <span class="conn-label">${escapeHtml(n.label)}</span>
                    <span class="conn-type-badge">${typeLabel}</span>
                `;
                div.addEventListener("click", () => {
                    if (!engramGraph.graph.hasNode(n.id)) {
                        loadNodeIntoGraph(n.id);
                    } else {
                        engramGraph.focusNode(n.id);
                        handleNodeClick(n.id);
                    }
                });
                group.appendChild(div);
            });

            if (neighbors.length > 30) {
                const more = document.createElement("div");
                more.className = "conn-item";
                more.style.color = "var(--text-muted)";
                more.style.fontStyle = "italic";
                more.textContent = `+${neighbors.length - 30} more`;
                group.appendChild(more);
            }

            connEl.appendChild(header);
            connEl.appendChild(group);
        }
    } catch (err) {
        console.error("Failed to load connections:", err);
    }
}

function handleStageClick() {
    closeSidebar();
}

function closeSidebar() {
    const sidebar = document.getElementById("sidebar");
    sidebar.classList.remove("visible");
    sidebar.classList.add("hidden");
    if (engramGraph) {
        engramGraph.selectedNode = null;
        engramGraph.highlightedNodes.clear();
        if (engramGraph.renderer) engramGraph.renderer.refresh();
    }
}

// ============================================================
// Utilities
// ============================================================

function showLoading() {
    document.getElementById("loading-overlay").classList.remove("hidden");
}

function hideLoading() {
    document.getElementById("loading-overlay").classList.add("hidden");
}

function updateConnectionStatus(connected) {
    const el = document.getElementById("connection-status");
    if (connected) {
        el.classList.remove("error");
        el.querySelector(".status-text").textContent = "Connected";
    } else {
        el.classList.add("error");
        el.querySelector(".status-text").textContent = "Disconnected";
    }
}

function formatDate(dateStr) {
    if (!dateStr) return '';
    try {
        const d = new Date(dateStr);
        if (isNaN(d.getTime())) return '';

        const now = new Date();
        const diffMs = now - d;
        const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

        if (diffDays === 0) {
            return 'Today';
        } else if (diffDays === 1) {
            return 'Yesterday';
        } else if (diffDays < 7) {
            return `${diffDays}d ago`;
        } else {
            return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: d.getFullYear() !== now.getFullYear() ? 'numeric' : undefined });
        }
    } catch (e) {
        return '';
    }
}

function getBgClass(type) {
    const map = {
        Entity: "bg-entity",
        Fact: "bg-fact",
        Episode: "bg-episode",
        Emotion: "bg-emotion",
        SessionState: "bg-session",
    };
    return map[type] || "bg-session";
}

function escapeAttr(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
