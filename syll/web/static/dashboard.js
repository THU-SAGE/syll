// Lynx Memory Dashboard - Frontend Logic

const AGENT_ICONS = {
    im_agent: '💬',
    gui_agent: '🖱️',
    memory_agent: '🧠',
    monitor_agent: '👁️'
};

const AGENT_COLORS = {
    im_agent: '#60a5fa',
    gui_agent: '#34d399',
    memory_agent: '#a78bfa',
    monitor_agent: '#fb923c'
};

async function initDashboard() {
    await loadStats();
    await loadHeatmap();
    await loadTimeline();
    await loadAgentChart();
}

async function loadStats() {
    try {
        const response = await fetch('/api/v1/dashboard/stats');
        const data = await response.json();

        document.getElementById('today-count').textContent = data.today_count;
        document.getElementById('total-count').textContent = data.total_count.toLocaleString();
        document.getElementById('active-agents').textContent = data.active_agents;

        if (data.last_update) {
            const date = new Date(data.last_update);
            document.getElementById('last-update').textContent = date.toLocaleDateString();
        } else {
            document.getElementById('last-update').textContent = 'N/A';
        }
    } catch (error) {
        console.error('Failed to load stats:', error);
    }
}

async function loadHeatmap() {
    try {
        const year = new Date().getFullYear();
        const response = await fetch(`/api/v1/dashboard/heatmap?year=${year}`);
        const data = await response.json();

        // Convert data to Cal-Heatmap format
        const heatmapData = Object.entries(data).map(([date, count]) => ({
            date: new Date(date).getTime() / 1000,
            value: count
        }));

        const cal = new CalHeatmap();
        cal.paint({
            itemSelector: '#cal-heatmap',
            domain: {
                type: 'month',
                gutter: 10,
                label: { text: 'MMM', textAlign: 'start', position: 'top' }
            },
            subDomain: { type: 'day', radius: 2, width: 12, height: 12, gutter: 4 },
            date: { start: new Date(year, 0, 1) },
            data: {
                source: heatmapData,
                x: 'date',
                y: 'value'
            },
            scale: {
                color: {
                    type: 'threshold',
                    range: ['#0e4429', '#006d32', '#26a641', '#39d353', '#34d399'],
                    domain: [1, 5, 10, 20]
                }
            },
            theme: 'dark'
        }, [
            [
                CalHeatmap.Tooltip,
                {
                    text: function (date, value, dayjsDate) {
                        return `${dayjsDate.format('YYYY-MM-DD')}: ${value || 0} events`;
                    }
                }
            ]
        ]);

        // Add click handler to load day detail
        document.querySelectorAll('#cal-heatmap rect').forEach(rect => {
            rect.addEventListener('click', async (e) => {
                const date = e.target.getAttribute('data-date');
                if (date) {
                    await loadDayDetail(date);
                }
            });
        });
    } catch (error) {
        console.error('Failed to load heatmap:', error);
    }
}

async function loadTimeline() {
    try {
        const response = await fetch('/api/v1/dashboard/timeline?limit=10');
        const events = await response.json();

        const timeline = document.getElementById('timeline');
        timeline.innerHTML = events.map(event => `
            <div class="timeline-item" onclick="loadEventDetail('${event.id}')">
                <div class="timeline-icon">${AGENT_ICONS[event.agent_type]}</div>
                <div class="timeline-content">
                    <div class="timeline-time">${formatTimestamp(event.timestamp)}</div>
                    <div class="timeline-summary">${event.summary}</div>
                </div>
            </div>
        `).join('');
    } catch (error) {
        console.error('Failed to load timeline:', error);
    }
}

async function loadAgentChart() {
    try {
        const response = await fetch('/api/v1/dashboard/stats');
        const data = await response.json();

        // Get agent distribution from recent events
        const timelineResponse = await fetch('/api/v1/dashboard/timeline?limit=100');
        const events = await timelineResponse.json();

        const agentCounts = {};
        events.forEach(event => {
            agentCounts[event.agent_type] = (agentCounts[event.agent_type] || 0) + 1;
        });

        const ctx = document.getElementById('agent-chart').getContext('2d');
        new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: Object.keys(agentCounts).map(key => key.replace('_agent', '').toUpperCase()),
                datasets: [{
                    data: Object.values(agentCounts),
                    backgroundColor: Object.keys(agentCounts).map(key => AGENT_COLORS[key]),
                    borderWidth: 2,
                    borderColor: '#1e293b'
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: true,
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: { color: '#f1f5f9' }
                    }
                }
            }
        });
    } catch (error) {
        console.error('Failed to load agent chart:', error);
    }
}

async function loadDayDetail(dateStr) {
    try {
        const response = await fetch(`/api/v1/dashboard/day/${dateStr}`);
        const data = await response.json();

        const detailDiv = document.getElementById('day-detail');
        const contentDiv = document.getElementById('day-content');

        // Show section
        detailDiv.style.display = 'block';

        // Render content
        contentDiv.innerHTML = `
            <div class="day-header">
                <h3>${dateStr} (${data.total} events)</h3>
                <div class="filter-buttons">
                    <button class="filter-btn active" onclick="filterEvents('all')">All</button>
                    <button class="filter-btn" onclick="filterEvents('im_agent')">IM</button>
                    <button class="filter-btn" onclick="filterEvents('gui_agent')">GUI</button>
                    <button class="filter-btn" onclick="filterEvents('memory_agent')">Memory</button>
                    <button class="filter-btn" onclick="filterEvents('monitor_agent')">Monitor</button>
                </div>
            </div>
            <div class="chart-container">
                <canvas id="hourly-chart"></canvas>
            </div>
            <div class="event-list">
                ${data.events.map(event => `
                    <div class="event-item" data-agent="${event.agent_type}">
                        <div class="event-icon">${AGENT_ICONS[event.agent_type]}</div>
                        <div class="event-content">
                            <div class="event-time">${formatTimestamp(event.timestamp)}</div>
                            <div class="event-summary">${event.summary}</div>
                            ${event.has_media ? '<span class="media-badge">📷</span>' : ''}
                        </div>
                        <button class="event-expand" onclick="loadEventDetail('${event.id}')">→</button>
                    </div>
                `).join('')}
            </div>
        `;

        // Render hourly chart
        const ctx = document.getElementById('hourly-chart').getContext('2d');
        new Chart(ctx, {
            type: 'bar',
            data: {
                labels: Array.from({length: 24}, (_, i) => `${i}:00`),
                datasets: [{
                    label: 'Events per Hour',
                    data: data.hourly_distribution,
                    backgroundColor: 'rgba(52, 211, 153, 0.6)',
                    borderColor: 'rgba(52, 211, 153, 1)',
                    borderWidth: 1
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: {
                        beginAtZero: true,
                        ticks: { color: '#9ca3af' },
                        grid: { color: '#374151' }
                    },
                    x: {
                        ticks: { color: '#9ca3af' },
                        grid: { color: '#374151' }
                    }
                },
                plugins: {
                    legend: { display: false }
                }
            }
        });

        // Scroll to day detail
        detailDiv.scrollIntoView({ behavior: 'smooth' });
    } catch (error) {
        console.error('Failed to load day detail:', error);
    }
}

function filterEvents(agentType) {
    // Update button states
    document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.classList.remove('active');
    });
    event.target.classList.add('active');

    // Filter events
    document.querySelectorAll('.event-item').forEach(item => {
        if (agentType === 'all' || item.dataset.agent === agentType) {
            item.style.display = 'flex';
        } else {
            item.style.display = 'none';
        }
    });
}

async function loadEventDetail(eventId) {
    try {
        const response = await fetch(`/api/v1/dashboard/events/${eventId}`);
        const event = await response.json();

        // Create modal content
        const modal = document.createElement('div');
        modal.className = 'modal';
        modal.innerHTML = `
            <h2>${AGENT_ICONS[event.agent_type]} ${event.agent_type.replace('_', ' ').toUpperCase()}</h2>
            <p><strong>Time:</strong> ${formatTimestamp(event.timestamp)}</p>
            <p><strong>Platform:</strong> ${event.source.platform}</p>
            <p><strong>Type:</strong> ${event.event_type}</p>
            ${event.tags.length > 0 ? `<p><strong>Tags:</strong> ${event.tags.join(', ')}</p>` : ''}
            ${event.summary ? `<p><strong>Summary:</strong> ${event.summary}</p>` : ''}
            <hr style="margin: 1rem 0; border-color: #475569;">
            <div style="white-space: pre-wrap;">${event.content.text || 'No content'}</div>
            ${event.content.media.length > 0 ? `
                <hr style="margin: 1rem 0; border-color: #475569;">
                <p><strong>Media:</strong></p>
                ${event.content.media.map(path => `<p>📷 ${path}</p>`).join('')}
            ` : ''}
            <button onclick="this.parentElement.remove()" style="margin-top: 1rem; padding: 0.5rem 1rem; background: #ef4444; border: none; border-radius: 6px; color: white; cursor: pointer;">Close</button>
        `;

        document.body.appendChild(modal);
    } catch (error) {
        console.error('Failed to load event detail:', error);
    }
}

function formatTimestamp(isoString) {
    const date = new Date(isoString);
    return date.toLocaleString('en-US', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
    });
}
