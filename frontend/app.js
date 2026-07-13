const DEFAULT_TOKEN = "supersecretapitoken";
let apiToken = localStorage.getItem("dns_failover_api_token") || DEFAULT_TOKEN;
let speechEnabled = localStorage.getItem("dns_failover_speech") !== "false";


// DOM Elements
const refreshBadge = document.getElementById("refresh-badge");
const authToggleBtn = document.getElementById("auth-toggle-btn");
const authModal = document.getElementById("auth-modal");
const apiTokenInput = document.getElementById("api-token-input");
const saveTokenBtn = document.getElementById("save-token-btn");
const closeModalBtn = document.getElementById("close-modal-btn");

const statusRing = document.getElementById("status-ring");
const systemStatusText = document.getElementById("system-status-text");
const systemStatusDesc = document.getElementById("system-status-desc");

const dbLatencyVal = document.getElementById("db-latency-val");
const dbEngineVal = document.getElementById("db-engine-val");
const cpuVal = document.getElementById("cpu-val");
const cpuBar = document.getElementById("cpu-bar");
const memoryVal = document.getElementById("memory-val");
const memoryBar = document.getElementById("memory-bar");

const poolsList = document.getElementById("pools-list");
const logsContainer = document.getElementById("logs-container");
const clearLogsBtn = document.getElementById("clear-logs-btn");

const btnForcePrimary = document.getElementById("btn-force-primary");
const btnForceBackup = document.getElementById("btn-force-backup");
const btnForceBoth = document.getElementById("btn-force-both");
const btnSimulateFailover = document.getElementById("btn-simulate-failover");
const voiceToggleBtn = document.getElementById("voice-toggle-btn");
const overrideStatusMessage = document.getElementById("override-status-message");
const toastContainer = document.getElementById("toast-container");

function showToast(message, type = "info") {
    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    
    let icon = "ℹ️";
    if (type === "success") icon = "✅";
    if (type === "error") icon = "❌";
    if (type === "warning") icon = "⚠️";
    
    toast.innerHTML = `
        <span class="toast-icon">${icon}</span>
        <span class="toast-message">${message}</span>
    `;
    
    toastContainer.appendChild(toast);
    
    setTimeout(() => {
        toast.remove();
    }, 4000);
}

// Visibility state tracking for idle throttle
let refreshIntervalId = null;

// Auth Modal Controls
authToggleBtn.addEventListener("click", () => {
    apiTokenInput.value = apiToken === DEFAULT_TOKEN ? "" : apiToken;
    authModal.classList.add("active");
});

closeModalBtn.addEventListener("click", () => {
    authModal.classList.remove("active");
});

saveTokenBtn.addEventListener("click", () => {
    const val = apiTokenInput.value.trim();
    if (val) {
        apiToken = val;
        localStorage.setItem("dns_failover_api_token", val);
        showToast("Bearer token configured", "success");
    } else {
        apiToken = DEFAULT_TOKEN;
        localStorage.removeItem("dns_failover_api_token");
        showToast("Bearer token reset to default", "warning");
    }
    authModal.classList.remove("active");
    fetchDashboardData();
    // Reconnect WebSocket with new token
    connectWebSocket();
});

// Helper for HTTP requests
async function apiRequest(method, endpoint, body = null) {
    const headers = {
        "Authorization": `Bearer ${apiToken}`,
        "Content-Type": "application/json"
    };
    
    const config = {
        method: method,
        headers: headers
    };
    
    if (body) {
        config.body = JSON.stringify(body);
    }
    
    try {
        const response = await fetch(endpoint, config);
        if (response.status === 401) {
            // Trigger auth modal
            authModal.classList.add("active");
            showToast("Unauthorized. Please configure a valid Bearer token.", "error");
            throw new Error("Unauthorized: Invalid API Token");
        }
        if (!response.ok) {
            const errJson = await response.json().catch(() => ({}));
            throw new Error(errJson.detail || `HTTP Error ${response.status}`);
        }
        return await response.json();
    } catch (e) {
        console.error(`API Call failed to ${endpoint}:`, e);
        throw e;
    }
}

// Fetch all status data
async function fetchDashboardData() {
    refreshBadge.textContent = "Syncing";
    refreshBadge.className = "badge syncing";
    
    try {
        const health = await apiRequest("GET", "/api/v1/health/failover");
        updateMetricsUI(health);
        
        const logs = await apiRequest("GET", "/api/v1/failover/logs");
        updateLogsUI(logs);
    } catch (e) {
        // Handle API token configuration modal trigger or failure
        overrideStatusMessage.textContent = `Sync Error: ${e.message}`;
        overrideStatusMessage.style.color = "#ff3838";
        showToast(`Sync Failed: ${e.message}`, "error");
    } finally {
        setTimeout(() => {
            refreshBadge.textContent = "Idle";
            refreshBadge.className = "badge idle";
        }, 800);
    }
}

function updateMetricsUI(data) {
    // 1. Overall health status
    if (data.status === "healthy") {
        statusRing.className = "status-indicator-ring healthy";
        systemStatusText.textContent = "HEALTHY";
        systemStatusText.style.color = "#00f2fe";
        systemStatusDesc.textContent = `All systems routing normally. Database is running in ${data.database_status} mode.`;
    } else {
        statusRing.className = "status-indicator-ring degraded";
        systemStatusText.textContent = "DEGRADED";
        systemStatusText.style.color = "#ff3838";
        systemStatusDesc.textContent = `Database connection degraded or operating in fallback mode. Active databases: ${data.database_status}.`;
    }
    
    // 2. Metrics
    dbLatencyVal.textContent = data.db_latency_ms;
    dbEngineVal.textContent = data.database_status;
    if (data.database_status === "primary") {
        dbEngineVal.className = "gradient-text-blue";
    } else {
        dbEngineVal.className = "gradient-text";
        dbEngineVal.style.color = "#ff3838";
    }
    
    cpuVal.textContent = data.cpu_percent;
    cpuBar.style.width = `${data.cpu_percent}%`;
    
    memoryVal.textContent = data.memory_percent;
    memoryBar.style.width = `${data.memory_percent}%`;
    
    // 3. Cloudflare load balancer pools
    poolsList.innerHTML = "";
    if (data.cloudflare_pool_status && data.cloudflare_pool_status.length > 0) {
        data.cloudflare_pool_status.forEach(pool => {
            const row = document.createElement("div");
            row.className = "pool-row";
            
            const isHealthy = pool.healthy;
            
            row.innerHTML = `
                <div class="pool-info">
                    <h4>${pool.name}</h4>
                    <div class="pool-sub">Origins: ${pool.origins.map(o => `${o.name} (${o.address})`).join(", ")}</div>
                </div>
                <span class="pool-health-badge ${isHealthy ? 'healthy' : 'unhealthy'}">
                    ${isHealthy ? 'HEALTHY' : 'UNHEALTHY'}
                </span>
            `;
            poolsList.appendChild(row);
        });
    } else {
        poolsList.innerHTML = `<div class="pool-item loading">No Cloudflare pools active or configured.</div>`;
    }

    // 4. Update Database Redundancy Chain
    const dbChainList = document.getElementById("db-chain-list");
    const activeDbNameLabel = document.getElementById("active-db-name");
    
    if (dbChainList && data.database_status_list) {
        dbChainList.innerHTML = "";
        let activeDbName = "Unknown";
        
        data.database_status_list.forEach((db, index) => {
            if (db.active) {
                activeDbName = db.name;
            }
            
            // Render database node
            const node = document.createElement("div");
            const isOffline = db.status === 'offline';
            const isManuallyDisabled = db.disabled;
            node.className = `db-node ${db.active ? 'active' : ''} ${isOffline ? 'offline' : ''} ${isManuallyDisabled ? 'manually-disabled' : ''}`;
            
            const statusText = isManuallyDisabled ? 'Disabled' : (isOffline ? 'Offline' : 'Healthy');
            const statusClass = isManuallyDisabled ? 'offline' : (isOffline ? 'offline' : 'healthy');
            const dotClass = isManuallyDisabled ? 'offline' : (isOffline ? 'offline' : 'healthy');
            
            node.innerHTML = `
                <div class="db-node-title">${db.name}</div>
                <div class="db-node-status ${statusClass}">
                    <span class="db-status-dot ${dotClass}"></span>
                    ${statusText}
                </div>
                ${db.active ? '<span class="db-active-badge">Active</span>' : ''}
                <button class="db-toggle-btn ${isManuallyDisabled ? 'enable-btn' : 'disable-btn'}" data-index="${index}" title="${isManuallyDisabled ? 'تفعيل قاعدة البيانات وإعادتها للخدمة' : 'محاكاة عطل وتوقف قاعدة البيانات عن العمل'}">
                    ${isManuallyDisabled ? '⚡ Enable' : '🔌 Outage'}
                </button>
            `;
            
            dbChainList.appendChild(node);
            
            // Add connection arrow if not the last node
            if (index < data.database_status_list.length - 1) {
                const arrow = document.createElement("div");
                arrow.className = "db-arrow";
                arrow.innerHTML = "➜";
                dbChainList.appendChild(arrow);
            }
        });
        
        // Add click events to the simulation buttons
        dbChainList.querySelectorAll(".db-toggle-btn").forEach(btn => {
            btn.addEventListener("click", async (e) => {
                e.stopPropagation();
                const index = parseInt(btn.dataset.index);
                const isCurrentlyDisabled = btn.classList.contains("enable-btn");
                const shouldEnable = isCurrentlyDisabled;
                
                try {
                    await apiRequest("POST", "/api/v1/database/toggle", {
                        index: index,
                        enabled: shouldEnable
                    });
                    showToast(`${shouldEnable ? 'Enabled' : 'Simulating outage for'} database node "${data.database_status_list[index].name}"`, "success");
                    fetchDashboardData();
                } catch (err) {
                    showToast(`Failed to toggle database simulation: ${err.message}`, "error");
                }
            });
        });
        
        if (activeDbNameLabel) {
            activeDbNameLabel.textContent = activeDbName;
        }
    }
}

function updateLogsUI(logs) {
    logsContainer.innerHTML = "";
    if (logs && logs.length > 0) {
        let lastLogLine = null;
        let lastLogKey = "";
        let duplicateCount = 1;

        logs.forEach(log => {
            const logDate = new Date(log.timestamp);
            const timeStr = logDate.toLocaleTimeString();
            const logKey = `${log.event_type}:${log.message}`;

            if (logKey === lastLogKey && lastLogLine) {
                duplicateCount++;
                lastLogLine.dataset.dupCount = duplicateCount;
                const countBadge = lastLogLine.querySelector(".log-count-badge");
                if (countBadge) {
                    countBadge.textContent = `x${duplicateCount}`;
                } else {
                    const badge = document.createElement("span");
                    badge.className = "log-count-badge";
                    badge.textContent = `x${duplicateCount}`;
                    lastLogLine.appendChild(badge);
                }
            } else {
                duplicateCount = 1;
                lastLogKey = logKey;
                
                const line = document.createElement("div");
                line.className = "log-line";
                line.dataset.logKey = logKey;
                line.dataset.dupCount = "1";
                line.innerHTML = `
                    <span class="log-time">[${timeStr}]</span>
                    <span class="log-type ${log.event_type}">${log.event_type}</span>
                    <span class="log-msg">${log.message}</span>
                `;
                logsContainer.appendChild(line);
                lastLogLine = line;
            }
        });
    } else {
        logsContainer.innerHTML = `<div style="color: var(--text-muted)">No logs recorded yet.</div>`;
    }
}

// Secure override confirmation popup timer (S04.20)
let overrideIntervalId = null;

function showOverrideModal(message, onConfirm) {
    const modal = document.getElementById("override-confirm-modal");
    const warningText = document.getElementById("override-warning-text");
    const cooldownText = document.getElementById("override-cooldown-text");
    const dismissText = document.getElementById("override-dismiss-text");
    const dismissBar = document.getElementById("override-dismiss-bar");
    const confirmBtn = document.getElementById("confirm-override-btn");
    const cancelBtn = document.getElementById("cancel-override-btn");

    warningText.textContent = `You are about to trigger manual failover override: "${message}"`;
    confirmBtn.disabled = true;
    confirmBtn.textContent = "Confirm (5s)";
    cooldownText.textContent = "Please wait 5s to confirm...";
    cooldownText.style.color = "var(--color-degraded)";
    dismissBar.style.width = "100%";
    dismissText.textContent = "Auto-cancelling in 15 seconds...";

    modal.classList.add("active");

    let cooldownMs = 5000;
    let autoCancelMs = 15000;
    const intervalMs = 100;

    clearInterval(overrideIntervalId);
    overrideIntervalId = setInterval(() => {
        autoCancelMs -= intervalMs;
        if (cooldownMs > 0) {
            cooldownMs -= intervalMs;
        }

        // Update progress bar
        const pct = (autoCancelMs / 15000) * 100;
        dismissBar.style.width = `${Math.max(0, pct)}%`;
        dismissText.textContent = `Auto-cancelling in ${Math.ceil(autoCancelMs / 1000)} seconds...`;

        if (cooldownMs > 0) {
            const secsLeft = Math.ceil(cooldownMs / 1000);
            confirmBtn.textContent = `Confirm (${secsLeft}s)`;
            cooldownText.textContent = `Please wait ${secsLeft}s to confirm...`;
        } else {
            if (confirmBtn.disabled) {
                confirmBtn.disabled = false;
                confirmBtn.textContent = "Confirm Override";
                cooldownText.textContent = "Override authorized. Proceed when ready.";
                cooldownText.style.color = "var(--color-healthy)";
            }
        }

        if (autoCancelMs <= 0) {
            cleanup();
            showToast("Override confirmation timed out", "warning");
        }
    }, intervalMs);

    const handleKeyDown = (e) => {
        if (e.key === "Escape") {
            cleanup();
            showToast("Override cancelled by user", "info");
        }
    };
    document.addEventListener("keydown", handleKeyDown);

    function cleanup() {
        clearInterval(overrideIntervalId);
        modal.classList.remove("active");
        document.removeEventListener("keydown", handleKeyDown);
    }

    confirmBtn.onclick = () => {
        cleanup();
        onConfirm();
    };

    cancelBtn.onclick = () => {
        cleanup();
        showToast("Override cancelled by user", "info");
    };
}

// Trigger manual overrides
async function executeOverride(primaryEnabled, backupEnabled, buttonText) {
    showOverrideModal(buttonText, async () => {
        overrideStatusMessage.textContent = "Sending override requests...";
        overrideStatusMessage.style.color = "var(--color-healthy)";
        
        try {
            const res = await apiRequest("POST", "/api/v1/failover/trigger", {
                primary_enabled: primaryEnabled,
                backup_enabled: backupEnabled
            });
            
            overrideStatusMessage.textContent = `Success: ${res.message}`;
            overrideStatusMessage.style.color = "var(--color-healthy)";
            showToast(`Override Success: ${res.message}`, "success");
            
            // Refresh after trigger
            fetchDashboardData();
        } catch (e) {
            overrideStatusMessage.textContent = `Override Failed: ${e.message}`;
            overrideStatusMessage.style.color = "#ff3838";
            showToast(`Override Failed: ${e.message}`, "error");
        }
    });
}

// Dark/light mode toggle with localStorage persistence (S04.05)
const themeToggleBtn = document.getElementById("theme-toggle-btn");

function initTheme() {
    const savedTheme = localStorage.getItem("dns_failover_theme") || "dark";
    setTheme(savedTheme);
}

function setTheme(theme) {
    if (theme === "light") {
        document.documentElement.setAttribute("data-theme", "light");
        themeToggleBtn.textContent = "🌙";
        themeToggleBtn.title = "Switch to Dark Mode";
    } else {
        document.documentElement.removeAttribute("data-theme");
        themeToggleBtn.textContent = "☀️";
        themeToggleBtn.title = "Switch to Light Mode";
    }
    localStorage.setItem("dns_failover_theme", theme);
}

themeToggleBtn.addEventListener("click", () => {
    const currentTheme = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    const newTheme = currentTheme === "light" ? "dark" : "light";
    setTheme(newTheme);
    showToast(`Theme switched to ${newTheme} mode`, "success");
});

btnForcePrimary.addEventListener("click", () => executeOverride(true, false, "Force Primary Only"));
btnForceBackup.addEventListener("click", () => executeOverride(false, true, "Force Backup Only"));
btnForceBoth.addEventListener("click", () => executeOverride(true, true, "Enable All Pools"));

clearLogsBtn.addEventListener("click", () => fetchDashboardData());

// Setup visibility status polling
function startPolling() {
    fetchDashboardData();
    refreshIntervalId = setInterval(() => {
        if (document.visibilityState === "visible") {
            fetchDashboardData();
        }
    }, 10000); // Poll every 10 seconds
}

function stopPolling() {
    if (refreshIntervalId) {
        clearInterval(refreshIntervalId);
        refreshIntervalId = null;
    }
}

// visibilitychange handler
document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
        startPolling();
    } else {
        stopPolling();
    }
});

// App Launch
initTheme();
initVoiceAlerts();
startPolling();

// Speech Synthesis Voice Alerts Logic


function initVoiceAlerts() {
    updateVoiceButtonUI();
    voiceToggleBtn.addEventListener("click", () => {
        speechEnabled = !speechEnabled;
        localStorage.setItem("dns_failover_speech", speechEnabled);
        updateVoiceButtonUI();
        const state = speechEnabled ? "enabled" : "muted";
        showToast(`Voice alerts ${state}`, "success");
        if (speechEnabled) {
            speakText("Voice alerts enabled");
        }
    });
}

function updateVoiceButtonUI() {
    if (speechEnabled) {
        voiceToggleBtn.textContent = "🔊";
        voiceToggleBtn.title = "Mute Voice Alerts";
    } else {
        voiceToggleBtn.textContent = "🔇";
        voiceToggleBtn.title = "Unmute Voice Alerts";
    }
}

function speakText(text) {
    if (!("speechSynthesis" in window)) return;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1.0;
    utterance.pitch = 1.0;
    window.speechSynthesis.speak(utterance);
}

function speakAlert(eventType, message) {
    if (!speechEnabled) return;
    
    let textToSpeak = "";
    if (eventType === "DB_FALLBACK_ACTIVE") {
        textToSpeak = "Alert: Primary database connection lost. Falling back to local database.";
    } else if (eventType === "DB_RESTORED") {
        textToSpeak = "Notice: Primary database connection restored.";
    } else if (eventType === "FAILOVER_TRIGGERED") {
        textToSpeak = `Warning: Manual routing override triggered. ${message || ""}`;
    } else if (eventType === "FAILOVER_SIMULATION") {
        textToSpeak = `Simulation: Failover dry run executed. ${message || ""}`;
    } else if (eventType === "CIRCUIT_BREAKER_TRIPPED") {
        textToSpeak = "Alert: Database connection pool circuit breaker tripped.";
    } else if (eventType === "CIRCUIT_BREAKER_RESET") {
        textToSpeak = "Notice: Database connection pool circuit breaker reset.";
    } else if (eventType === "SYSTEM_DEGRADED") {
        textToSpeak = "Warning: System health status is degraded.";
    } else if (eventType === "SYSTEM_HEALTHY") {
        textToSpeak = "Notice: System health status is healthy.";
    } else {
        textToSpeak = `${eventType.replace(/_/g, " ")}. ${message || ""}`;
    }
    
    speakText(textToSpeak);
}

// Dry Run Simulation Trigger
if (btnSimulateFailover) {
    btnSimulateFailover.addEventListener("click", () => {
        showOverrideModal("Simulate Failover (Dry Run)", async () => {
            overrideStatusMessage.textContent = "Sending simulation request...";
            overrideStatusMessage.style.color = "#ff9f43";
            
            try {
                const res = await apiRequest("POST", "/api/v1/failover/simulate", {
                    primary_enabled: false,
                    backup_enabled: true
                });
                
                overrideStatusMessage.textContent = `Simulation Success: ${res.message}`;
                overrideStatusMessage.style.color = "#ff9f43";
                showToast(`Simulation Triggered: ${res.message}`, "success");
                
                fetchDashboardData();
            } catch (e) {
                overrideStatusMessage.textContent = `Simulation Failed: ${e.message}`;
                overrideStatusMessage.style.color = "#ff3838";
                showToast(`Simulation Failed: ${e.message}`, "error");
            }
        });
    });
}

// Keyboard Shortcuts: Close modals on Escape key press (S04.27)
document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
        const authModal = document.getElementById("auth-modal");
        const overrideModal = document.getElementById("override-confirm-modal");
        
        if (authModal.classList.contains("active")) {
            authModal.classList.remove("active");
        }
        if (overrideModal.classList.contains("active")) {
            // Cancel override if active
            clearInterval(overrideIntervalId);
            overrideModal.classList.remove("active");
            showToast("Override configuration cancelled", "info");
        }
    }
});

// ─── WebSocket Real-Time Log Feed (M3) ────────────────────────────────────────
let wsConnection = null;
let wsReconnectTimeout = null;
let wsConnected = false;
let wsReconnectAttempts = 0;

function getWsUrl() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/api/v1/logs/ws?token=${encodeURIComponent(apiToken)}`;
}

function connectWebSocket() {
    if (wsConnection) {
        wsConnection.close();
        wsConnection = null;
    }
    clearTimeout(wsReconnectTimeout);

    try {
        wsConnection = new WebSocket(getWsUrl());
    } catch (e) {
        scheduleWsReconnect();
        return;
    }

    wsConnection.onopen = () => {
        wsConnected = true;
        wsReconnectAttempts = 0; // Reset reconnection attempts upon success
        console.log("[WS] Connected to real-time log feed");
    };

    wsConnection.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            // Ignore heartbeat pings
            if (data.type === "ping") return;
            // Queue live log entries for batched UI rendering
            if (data.event_type && data.message) {
                queueLogEntry(data);
            }
        } catch (e) {
            // Ignore malformed messages
        }
    };

    wsConnection.onclose = () => {
        wsConnected = false;
        scheduleWsReconnect();
    };

    wsConnection.onerror = () => {
        wsConnected = false;
        wsConnection = null;
        scheduleWsReconnect();
    };
}

function scheduleWsReconnect() {
    clearTimeout(wsReconnectTimeout);
    
    // Exponential backoff reconnect: double delay each time up to 5 seconds
    const baseDelay = Math.min(5000, 1000 * Math.pow(2, wsReconnectAttempts));
    // Add +/- 20% random jitter to smear thundering herds
    const jitter = baseDelay * 0.2 * (Math.random() * 2 - 1);
    const delay = Math.max(1000, baseDelay + jitter);
    
    console.log(`[WS] Reconnecting in ${Math.round(delay)}ms (attempt ${wsReconnectAttempts + 1})`);
    wsReconnectAttempts++;

    wsReconnectTimeout = setTimeout(() => {
        connectWebSocket();
    }, delay);
}

let pendingLogs = [];
let renderRequested = false;

function queueLogEntry(log) {
    pendingLogs.push(log);
    if (log.source === "live") {
        speakAlert(log.event_type, log.message);
    }
    if (!renderRequested) {
        renderRequested = true;
        requestAnimationFrame(renderPendingLogs);
    }
}

function renderPendingLogs() {
    renderRequested = false;
    if (pendingLogs.length === 0) return;

    const fragment = document.createDocumentFragment();

    for (const log of pendingLogs) {
        const logKey = `${log.event_type}:${log.message || ""}`;
        const ts = log.timestamp ? new Date(log.timestamp).toLocaleTimeString() : "now";
        
        // Deduplication/collapsing check across current fragment and existing DOM
        const firstChild = fragment.firstElementChild || logsContainer.firstElementChild;
        if (firstChild && firstChild.dataset.logKey === logKey) {
            let dupCount = parseInt(firstChild.dataset.dupCount || "1", 10) + 1;
            firstChild.dataset.dupCount = dupCount;
            
            let countBadge = firstChild.querySelector(".log-count-badge");
            if (!countBadge) {
                countBadge = document.createElement("span");
                countBadge.className = "log-count-badge";
                firstChild.appendChild(countBadge);
            }
            countBadge.textContent = `x${dupCount}`;
            continue;
        }

        const entry = document.createElement("div");
        entry.className = "log-line";
        entry.dataset.logKey = logKey;
        entry.dataset.dupCount = "1";
        entry.innerHTML = `
            <span class="log-time">[${ts}]</span>
            <span class="log-type ${log.event_type}">${log.event_type}</span>
            <span class="log-msg">${log.message || ""}</span>
        `;
        fragment.prepend(entry);
    }

    logsContainer.prepend(fragment);
    
    // Keep terminal capped at 100 entries
    while (logsContainer.children.length > 100) {
        logsContainer.removeChild(logsContainer.lastChild);
    }

    pendingLogs = [];
}

// Initialize WebSocket on load
connectWebSocket();



