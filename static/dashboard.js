document.addEventListener("DOMContentLoaded", function () {
    // Shared chart aesthetic configuration
    Chart.defaults.color = '#a0a5b8';
    Chart.defaults.font.family = 'Inter, sans-serif';
    Chart.defaults.scale.grid.color = 'rgba(255, 255, 255, 0.05)';
    Chart.defaults.scale.grid.borderColor = 'transparent';

    let allMonths = [];
    let selectedCategory = '';

    // Initialize fetching base stats to show group name
    fetch("/api/stats").then(r => r.json()).then(data => {
        if (data.group_name) {
            const groupName = document.getElementById('groupName');
            if (groupName) groupName.innerHTML = `<i class="fa-solid fa-chart-pie"></i> ${escapeHtml(data.group_name)}`;
        }
    });

    // Populate year and month dropdowns
    const years = [2022, 2023, 2024, 2025, 2026, 2027];
    const monthsList = [
        { value: '01', name: 'Jan' }, { value: '02', name: 'Feb' }, { value: '03', name: 'Mar' },
        { value: '04', name: 'Apr' }, { value: '05', name: 'May' }, { value: '06', name: 'Jun' },
        { value: '07', name: 'Jul' }, { value: '08', name: 'Aug' }, { value: '09', name: 'Sep' },
        { value: '10', name: 'Oct' }, { value: '11', name: 'Nov' }, { value: '12', name: 'Dec' }
    ];

    function populateDropdown(id, options, valueKey = 'value', textKey = 'name') {
        const dropdown = document.getElementById(id);
        if (!dropdown) return;
        dropdown.innerHTML = '';
        options.forEach(opt => {
            const option = document.createElement('option');
            option.value = opt[valueKey] || opt;
            option.textContent = opt[textKey] || opt;
            dropdown.appendChild(option);
        });
    }

    populateDropdown('startYearDropdown', years.map(y => ({ value: y, name: y })));
    populateDropdown('endYearDropdown', years.map(y => ({ value: y, name: y })));
    populateDropdown('startMonthDropdown', monthsList);
    populateDropdown('endMonthDropdown', monthsList);

    // Set defaults (start of range or something reasonable)
    const startYearDropdown = document.getElementById('startYearDropdown');
    const startMonthDropdown = document.getElementById('startMonthDropdown');
    const endYearDropdown = document.getElementById('endYearDropdown');
    const endMonthDropdown = document.getElementById('endMonthDropdown');
    
    // We will initialize values later based on actual data
    const now = new Date();
    const currentYearStr = now.getFullYear().toString();
    const currentMonthStr = (now.getMonth() + 1).toString().padStart(2, '0');

    function getSelectedRange() {
        let start = `${startYearDropdown.value}-${startMonthDropdown.value}`;
        let end = `${endYearDropdown.value}-${endMonthDropdown.value}`;
        if (start > end) {
            end = start; // Prevent invalid ranges visually jumping, handled in UI
        }
        return { start, end };
    }

    function syncDropdownsToRange(startStr, endStr) {
        if(startStr) {
            const [sy, sm] = startStr.split('-');
            if (startYearDropdown) startYearDropdown.value = sy;
            if (startMonthDropdown) startMonthDropdown.value = sm;
        }
        if(endStr) {
            const [ey, em] = endStr.split('-');
            if (endYearDropdown) endYearDropdown.value = ey;
            if (endMonthDropdown) endMonthDropdown.value = em;
        }
    }

    // Attach listeners
    [startYearDropdown, startMonthDropdown, endYearDropdown, endMonthDropdown].forEach(drop => {
        if (drop) drop.addEventListener('change', () => {
            let { start, end } = getSelectedRange();
            if (start > end) {
                syncDropdownsToRange(start, start); // force reset on UI
                end = start;
            }
            triggerDashboardUpdate(start, end, selectedCategory);
        });
    });

    const categoryFilter = document.getElementById('categoryFilter');
    if (categoryFilter) {
        categoryFilter.addEventListener('change', () => {
            selectedCategory = categoryFilter.value;
            let { start, end } = getSelectedRange();
            triggerDashboardUpdate(start, end, selectedCategory);
        });
    }

    // Reset button
    const resetFiltersBtn = document.getElementById('resetFiltersBtn');
    if (resetFiltersBtn) {
        resetFiltersBtn.addEventListener('click', () => {
            if (allMonths.length > 0) {
                const firstMonth = allMonths[0];
                const lastMonth = allMonths[allMonths.length - 1];
                syncDropdownsToRange(firstMonth, lastMonth);
                selectedCategory = '';
                if(categoryFilter) categoryFilter.value = '';
                triggerDashboardUpdate(firstMonth, lastMonth, selectedCategory);
            }
        });
    }

    // Initial Fetch Sequence
    fetch("/api/monthly").then(r => r.json()).then(months => {
        if (months.length > 0) {
            allMonths = months.map(m => m.month);
            // Default to first month to last month
            const firstM = allMonths[0];
            const lastM = allMonths[allMonths.length - 1];
            syncDropdownsToRange(firstM, lastM);
            triggerDashboardUpdate(firstM, lastM, selectedCategory);
        } else {
             // Fallback
             syncDropdownsToRange("2023-01", `${currentYearStr}-${currentMonthStr}`);
             triggerDashboardUpdate("2023-01", `${currentYearStr}-${currentMonthStr}`, selectedCategory);
        }
    });

    // Populate categories
    fetch('/api/categories').then(r => r.json()).then(data => {
        if (categoryFilter && data.categories) {
            data.categories.forEach(cat => {
                const opt = document.createElement('option');
                opt.value = cat;
                opt.textContent = cat;
                categoryFilter.appendChild(opt);
            });
        }
    });

    // Central update orchestrator
    function triggerDashboardUpdate(start, end, category) {
        updateStatsHeader(start, end, category);
        updateCategoryCumulativeChart(start, end, category);
        updateMonthwiseBarChart(start, end, category);
        loadTopExpenses(start, end, category);
        
        const recurringLimitInput = document.getElementById('recurringLimit');
        const recurringLimit = recurringLimitInput ? parseInt(recurringLimitInput.value, 10) : 5;
        updateTopRecurringExpenses(start, end, category, recurringLimit);
    }

    function updateStatsHeader(start, end, category) {
        let url = `/api/top-expenses/${start}/${end}`;
        let urlCategoryTots = `/api/category-totals?start=${start}&end=${end}`;
        if(category) {
            url += `?category=${encodeURIComponent(category)}`;
            urlCategoryTots += `&category=${encodeURIComponent(category)}`;
        }
        
        // Complex but precise logic to get stats for the current filter
        Promise.all([
            fetch(url).then(r=>r.json()), 
            fetch(urlCategoryTots).then(r=>r.json())
        ]).then(([expenses, categoryTotals]) => {
            const totalSpent = categoryTotals.reduce((sum, c) => sum + c.total, 0);
            const totalCount = expenses.length;
            
            // Calculate distinct months between start and end mathematically
            let sDate = new Date(start + "-01");
            let eDate = new Date(end + "-01");
            let numMonths = (eDate.getFullYear() - sDate.getFullYear()) * 12 + (eDate.getMonth() - sDate.getMonth()) + 1;
            
            const avgMonthly = numMonths > 0 ? totalSpent / numMonths : 0;

            const quickStats = document.getElementById("quickStats");
            if (quickStats) {
                quickStats.innerHTML = `
                    <div class="stat-card"><span>Filtered Total</span><strong><i class="fa-solid fa-indian-rupee-sign"></i> ${totalSpent.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong></div>
                    <div class="stat-card"><span>Monthly Avg</span><strong><i class="fa-solid fa-indian-rupee-sign"></i> ${avgMonthly.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong></div>
                    <div class="stat-card"><span>Total Trx</span><strong>${totalCount}</strong></div>
                `;
            }
        });
    }

    // Chart: Overall Trend (Always shows everything, but allows clicking to deep dive)
    fetch("/api/monthly-trend").then(r => r.json()).then(trend => {
        const ctx = document.getElementById('monthlyTrendChart')?.getContext('2d');
        if (!ctx) return;

        // Custom Gradient
        let gradient = ctx.createLinearGradient(0, 0, 0, 400);
        gradient.addColorStop(0, 'rgba(0, 210, 211, 0.4)');
        gradient.addColorStop(1, 'rgba(0, 210, 211, 0.0)');

        window.monthlyTrendChartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: trend.map(m => m.month),
                datasets: [{
                    label: 'Overall Spend',
                    data: trend.map(m => m.total),
                    borderColor: '#00d2d3',
                    backgroundColor: gradient,
                    borderWidth: 3,
                    fill: true,
                    tension: 0.4,
                    pointBackgroundColor: '#0f111a',
                    pointBorderColor: '#00d2d3',
                    pointBorderWidth: 2,
                    pointRadius: 4,
                    pointHoverRadius: 6
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: 'rgba(26, 29, 45, 0.9)',
                        titleColor: '#fff',
                        bodyColor: '#00d2d3',
                        padding: 12,
                        callbacks: {
                            label: function (context) { return `₹ ${context.parsed.y.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`; }
                        }
                    }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        ticks: { callback: value => '₹ ' + value.toLocaleString() }
                    }
                },
                onClick: (e, elements) => {
                    if (elements && elements.length) {
                        const idx = elements[0].index;
                        const month = trend[idx].month;
                        // Deep Dive into this month
                        syncDropdownsToRange(month, month);
                        // Reset category if they want to see full month
                        // selectedCategory = ''; 
                        // if(categoryFilter) categoryFilter.value = '';
                        triggerDashboardUpdate(month, month, selectedCategory);
                        // Smooth scroll back to top
                        window.scrollTo({ top: 0, behavior: 'smooth' });
                    }
                }
            }
        });
    });

    // Chart: Monthwise Bar Chart (Reflects current filters)
    function updateMonthwiseBarChart(start, end, category) {
        let url = `/api/monthly?start=${start}&end=${end}`;
        if (category) url += `&category=${encodeURIComponent(category)}`;
        fetch(url).then(r => r.json()).then(filtered => {
            const ctx = document.getElementById('monthlyBarChart')?.getContext('2d');
            if (!ctx) return;
            if (window.monthlyBarChartInstance && typeof window.monthlyBarChartInstance.destroy === 'function') {
                window.monthlyBarChartInstance.destroy();
            }
            
            // Build continuous axis
            const result = [];
            let current = new Date(start + '-01');
            const last = new Date(end + '-01');
            while (current <= last) {
                result.push(current.toISOString().slice(0, 7));
                current.setMonth(current.getMonth() + 1);
            }
            
            const monthTotals = {};
            filtered.forEach(m => { monthTotals[m.month] = m.total; });
            const dataMapped = result.map(m => ({ month: m, total: monthTotals[m] || 0 }));

            // Custom Bar Gradient
            let barGradient = ctx.createLinearGradient(0, 0, 0, 400);    
            barGradient.addColorStop(0, '#6c5ce7');   
            barGradient.addColorStop(1, 'rgba(108, 92, 231, 0.2)');

            window.monthlyBarChartInstance = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: dataMapped.map(m => m.month),
                    datasets: [{
                        label: 'Spend',
                        data: dataMapped.map(m => m.total),
                        backgroundColor: barGradient,
                        borderRadius: 6,
                        borderSkipped: false
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { 
                        legend: { display: false },
                        tooltip: {
                            backgroundColor: 'rgba(26, 29, 45, 0.9)',
                            padding: 12,
                            callbacks: {
                                label: function (context) { return `₹ ${context.parsed.y.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`; }
                            }
                        }
                    },
                    scales: {
                        y: {
                            beginAtZero: true,
                            ticks: { callback: value => '₹ ' + value.toLocaleString() }
                        }
                    },
                    onClick: (e, elements) => {
                        if (elements && elements.length) {
                            const idx = elements[0].index;
                            const month = dataMapped[idx].month;
                            syncDropdownsToRange(month, month);
                            triggerDashboardUpdate(month, month, selectedCategory);
                            window.scrollTo({ top: 0, behavior: 'smooth' });
                        }
                    }
                }
            });
        });
    }

    // Chart: Category Cumulative
    function updateCategoryCumulativeChart(start, end, category) {
        let url = `/api/category-totals?start=${start}&end=${end}`;
        if (category) url += `&category=${encodeURIComponent(category)}`;
        
        fetch(url).then(r => r.json()).then(data => {
            if (category) {
                // When a single category is selected, filtering backend returns just one.
                data = data.filter(c => c.category === category);
            }
            const ctx = document.getElementById('categoryCumulativeChart')?.getContext('2d');
            if (!ctx) return;
            if (window.categoryCumulativeChartInstance && typeof window.categoryCumulativeChartInstance.destroy === 'function') {
                window.categoryCumulativeChartInstance.destroy();
            }
            
            window.categoryCumulativeChartInstance = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: data.map(c => c.category),
                    datasets: [{
                        label: 'Total Spend',
                        data: data.map(c => c.total),
                        backgroundColor: data.map((_, i) => `hsla(${i * 45 % 360}, 75%, 65%, 0.8)`),
                        borderRadius: 4,
                    }]
                },
                options: {
                    indexAxis: 'y',
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            backgroundColor: 'rgba(26, 29, 45, 0.9)',
                            padding: 12,
                            callbacks: {
                                label: function (context) { return `₹ ${context.parsed.x.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`; }
                            }
                        }
                    },
                    scales: {
                        x: {
                            beginAtZero: true,
                            ticks: { callback: value => '₹ ' + value.toLocaleString() }
                        }
                    },
                    onClick: (e, elements) => {
                        if (elements && elements.length) {
                            const idx = elements[0].index;
                            const cat = data[idx].category;
                            selectedCategory = cat;
                            if(categoryFilter) categoryFilter.value = cat;
                            triggerDashboardUpdate(start, end, selectedCategory);
                            window.scrollTo({ top: 0, behavior: 'smooth' });
                        }
                    }
                }
            });
        });
    }

    // Top N expenses table
    function loadTopExpenses(start, end, category) {
        let url = `/api/top-expenses/${start}/${end}?limit=10`;
        if (category) url += `&category=${encodeURIComponent(category)}`;
        fetch(url).then(r => r.json()).then(expenses => {
            let html = '';
            if (!expenses.length) {
                html = '<div style="color: var(--text-muted); padding: 20px 0;">No expenses found for this selection.</div>';
            } else {
                html = '<table><thead><tr><th>Date</th><th>Description</th><th style="text-align: right;">Cost</th><th>Category</th></tr></thead><tbody>';
                expenses.forEach(e => {
                    html += `<tr>
                                <td>${e.date}</td>
                                <td><span style="font-weight: 500; color: #fff;">${escapeHtml(e.description)}</span></td>
                                <td style="text-align: right; color: var(--accent-secondary); font-weight: 600;">₹${e.cost.toLocaleString('en-IN', { minimumFractionDigits: 2 })}</td>
                                <td><span class="badge" style="background: rgba(108, 92, 231, 0.15); color: #a29bfe; border: 1px solid rgba(108, 92, 231, 0.3);"><i class="${getCategoryIcon(e.category)}"></i> ${escapeHtml(e.category)}</span></td>
                             </tr>`;
                });
                html += '</tbody></table>';
            }
            const el = document.getElementById('topExpenses');
            if (el) el.innerHTML = html;
        });
    }

    function updateTopRecurringExpenses(start, end, category, limit) {
        let url = `/api/top-recurring-expenses?start=${start}&end=${end}&limit=${limit}`;
        if (category) url += `&category=${encodeURIComponent(category)}`;
        fetch(url).then(r => r.json()).then(expenses => {
            let html = '';
            if (!expenses.length) {
                html = '<div style="color: var(--text-muted); padding: 20px 0;">No recurring expenses found.</div>';
            } else {
                html = '<table><thead><tr><th>Description</th><th style="text-align:center;">Months</th><th style="text-align:center;">Count</th><th style="text-align: right;">Total</th><th style="text-align: right;">Avg/Month</th></tr></thead><tbody>';
                expenses.forEach(e => {
                    html += `<tr>
                                <td><span style="font-weight: 500; color: #fff;">${escapeHtml(e.description)}</span></td>
                                <td style="text-align:center;">${e.months}</td>
                                <td style="text-align:center;">${e.count}</td>
                                <td style="text-align: right; font-weight: 600;">₹${e.total.toLocaleString('en-IN', { minimumFractionDigits: 2 })}</td>
                                <td style="text-align: right; color: var(--accent-secondary);">₹${e.avg_per_occurrence.toLocaleString('en-IN', { minimumFractionDigits: 2 })}</td>
                             </tr>`;
                });
                html += '</tbody></table>';
            }
            const el = document.getElementById('topRecurringExpenses');
            if (el) el.innerHTML = html;
        });
    }

    // Update repeating limit
    const recurringLimitInput = document.getElementById('recurringLimit');
    if (recurringLimitInput) {
        recurringLimitInput.addEventListener('change', () => {
            let { start, end } = getSelectedRange();
            updateTopRecurringExpenses(start, end, selectedCategory, parseInt(recurringLimitInput.value, 10) || 5);
        });
    }

    // Prediction Chart
    fetch("/api/predict").then(r => r.json()).then(data => {
        const ctx = document.getElementById('predictionChart')?.getContext('2d');
        if(!ctx) return;
        
        let predGradient = ctx.createLinearGradient(0, 0, 0, 400);    
        predGradient.addColorStop(0, 'rgba(253, 121, 168, 0.4)');   
        predGradient.addColorStop(1, 'rgba(253, 121, 168, 0.0)');

        new Chart(ctx, {
            type: 'line',
            data: {
                labels: data.prediction.map(p => p.month),
                datasets: [{
                    label: 'Predicted Spend',
                    data: data.prediction.map(p => p.predicted),
                    borderColor: '#fd79a8',
                    backgroundColor: predGradient,
                    fill: true,
                    borderDash: [5, 5],
                    pointBackgroundColor: '#0f111a',
                    pointBorderColor: '#fd79a8'
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: 'rgba(26, 29, 45, 0.9)',
                        callbacks: {
                            label: function(ctx) { return `₹ ${ctx.parsed.y.toLocaleString('en-IN')}`; }
                        }
                    }
                },
                scales: {
                    y: { beginAtZero: true, ticks: { callback: v => '₹ ' + v } }
                }
            }
        });
    });

    // FontAwesome Icon Mapping
    function getCategoryIcon(category) {
        const map = {
            'Food & Drink': 'fa-solid fa-utensils',
            'Shopping': 'fa-solid fa-cart-shopping',
            'Home': 'fa-solid fa-house',
            'Transportation': 'fa-solid fa-car',
            'Utilities': 'fa-solid fa-bolt',
            'Entertainment': 'fa-solid fa-film',
            'Travel': 'fa-solid fa-plane',
            'Personal': 'fa-solid fa-user',
            'Groceries': 'fa-solid fa-basket-shopping',
            'Health': 'fa-solid fa-heart-pulse',
            'General': 'fa-solid fa-receipt',
            'Rent': 'fa-solid fa-building',
            'Education': 'fa-solid fa-graduation-cap'
        };
        return map[category] || 'fa-solid fa-tag';
    }
});