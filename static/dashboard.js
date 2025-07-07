document.addEventListener("DOMContentLoaded", function() {
    // Sticky header quick stats
    fetch("/api/stats").then(r => r.json()).then(data => {
        const quickStats = document.getElementById("quickStats");
        function formatMonth(dateStr) {
            if (!dateStr) return '';
            const d = new Date(dateStr);
            return d.toLocaleString('en-US', { month: 'short', year: 'numeric' });
        }
        if (quickStats) {
            quickStats.innerHTML = `
                <div class="stat-card"><span>Total Spent</span><br><strong>₹${data.total_spent.toLocaleString('en-IN', {minimumFractionDigits: 2})}</strong></div>
                <div class="stat-card"><span>Average per Month</span><br><strong>₹${data.average_expense.toLocaleString('en-IN', {minimumFractionDigits: 2})}</strong></div>
                <div class="stat-card"><span>Date Range</span><br><strong>${formatMonth(data.date_range.start)} - ${formatMonth(data.date_range.end)}</strong></div>
                <div class="stat-card"><span>Number of Expenses</span><br><strong>${data.total_expenses}</strong></div>
            `;
        }
        if (data.group_name) {
            const groupName = document.getElementById('groupName');
            if (groupName) groupName.textContent = data.group_name;
        }
    });

    // Populate year and month dropdowns
    const years = [2023, 2024, 2025];
    const monthsList = [
        { value: '01', name: 'Jan' },
        { value: '02', name: 'Feb' },
        { value: '03', name: 'Mar' },
        { value: '04', name: 'Apr' },
        { value: '05', name: 'May' },
        { value: '06', name: 'Jun' },
        { value: '07', name: 'Jul' },
        { value: '08', name: 'Aug' },
        { value: '09', name: 'Sep' },
        { value: '10', name: 'Oct' },
        { value: '11', name: 'Nov' },
        { value: '12', name: 'Dec' }
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
    // Set defaults
    const startYearDropdown = document.getElementById('startYearDropdown');
    const startMonthDropdown = document.getElementById('startMonthDropdown');
    const endYearDropdown = document.getElementById('endYearDropdown');
    const endMonthDropdown = document.getElementById('endMonthDropdown');
    if (startYearDropdown) startYearDropdown.value = '2023';
    if (startMonthDropdown) startMonthDropdown.value = '01';
    const now = new Date();
    const currentYearStr = now.getFullYear().toString();
    const currentMonthStr = (now.getMonth() + 1).toString().padStart(2, '0');
    if (endYearDropdown) endYearDropdown.value = currentYearStr;
    if (endMonthDropdown) endMonthDropdown.value = currentMonthStr;

    function getSelectedRange() {
        const startYear = startYearDropdown.value;
        const startMonth = startMonthDropdown.value;
        const endYear = endYearDropdown.value;
        const endMonth = endMonthDropdown.value;
        // Snap end to current month if selected
        const now = new Date();
        const currentYear = now.getFullYear().toString();
        const currentMonth = (now.getMonth() + 1).toString().padStart(2, '0');
        let end = `${endYear}-${endMonth}`;
        if (endYear === currentYear && endMonth === currentMonth) {
            end = `${currentYear}-${currentMonth}`; // Snap to current month
        }
        return {
            start: `${startYear}-${startMonth}`,
            end: end
        };
    }
    [startYearDropdown, startMonthDropdown, endYearDropdown, endMonthDropdown].forEach(drop => {
        if (drop) drop.addEventListener('change', () => {
            let { start, end } = getSelectedRange();
            // Ensure start <= end
            if (start > end) {
                // If start > end, set end = start
                endYearDropdown.value = startYearDropdown.value;
                endMonthDropdown.value = startMonthDropdown.value;
                end = start;
            }
            updateDashboardForRange(start, end);
        });
    });

    // Section 2: Monthwise Bar Chart
    let allMonths = [];
    let monthsData = [];

    // Helper to generate all months between two dates (inclusive)
    function getAllMonthsBetween(start, end) {
        const result = [];
        let current = new Date(start.getFullYear(), start.getMonth(), 1);
        const last = new Date(end.getFullYear(), end.getMonth(), 1);
        while (current <= last) {
            result.push(current.toISOString().slice(0, 7));
            current.setMonth(current.getMonth() + 1);
        }
        return result;
    }

    // Fetch all months for dashboard logic
    fetch("/api/monthly").then(r => r.json()).then(months => {
        // allMonths is now just the months present in the backend response, in order
        allMonths = months.map(m => m.month);
        // Map monthsData to all months, filling missing with 0 (shouldn't be needed, but keep for safety)
        const monthTotals = {};
        months.forEach(m => { monthTotals[m.month] = m.total; });
        monthsData = allMonths.map(m => ({ month: m, total: monthTotals[m] || 0 }));
        // Set initial dashboard
        let { start, end } = getSelectedRange();
        updateDashboardForRange(start, end);
    });

    // Fetch and populate category filter
    fetch('/api/categories').then(r => r.json()).then(data => {
        const filter = document.getElementById('categoryFilter');
        if (filter && data.categories) {
            data.categories.forEach(cat => {
                const opt = document.createElement('option');
                opt.value = cat;
                opt.textContent = cat;
                filter.appendChild(opt);
            });
        }
    });

    let selectedCategory = '';
    const categoryFilter = document.getElementById('categoryFilter');
    if (categoryFilter) {
        categoryFilter.addEventListener('change', () => {
            selectedCategory = categoryFilter.value;
            let { start, end } = getSelectedRange();
            updateDashboardForRange(start, end);
        });
    }

    function updateDashboardForRange(start, end) {
        updateCategoryCumulativeChart(start, end, selectedCategory);
        updateMonthwiseBarChart(start, end, selectedCategory);
        loadMonthRangeDetail(start, end, selectedCategory);
    }

    function updateMonthwiseBarChart(start, end, category) {
        let url = `/api/monthly?start=${start}&end=${end}`;
        if (category) url += `&category=${encodeURIComponent(category)}`;
        fetch(url).then(r => r.json()).then(filtered => {
            if (!filtered.length) {
                renderMonthwiseBarChart([]);
                return;
            }
            // Generate all months between start and end
            function getAllMonthsBetween(start, end) {
                const result = [];
                let current = new Date(start + '-01');
                const last = new Date(end + '-01');
                while (current <= last) {
                    result.push(current.toISOString().slice(0, 7));
                    current.setMonth(current.getMonth() + 1);
                }
                return result;
            }
            const allMonthStrings = getAllMonthsBetween(start, end);
            // Map backend data to this range, filling missing with 0
            const monthTotals = {};
            filtered.forEach(m => { monthTotals[m.month] = m.total; });
            const mapped = allMonthStrings.map(m => ({ month: m, total: monthTotals[m] || 0 }));
            renderMonthwiseBarChart(mapped);
        });
    }

    function renderMonthwiseBarChart(data) {
        const ctx = document.getElementById('monthlyBarChart')?.getContext('2d');
        if (!ctx) return;
        if (window.monthlyBarChartInstance && typeof window.monthlyBarChartInstance.destroy === 'function') {
            window.monthlyBarChartInstance.destroy();
        }
        if (!data.length) {
            ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
            ctx.font = '16px Arial';
            ctx.fillText('No data for selected category/range', 20, 40);
            return;
        }
        window.monthlyBarChartInstance = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: data.map(m => m.month),
                datasets: [{
                    label: 'Total Spend (INR)',
                    data: data.map(m => m.total),
                    backgroundColor: 'rgba(54, 162, 235, 0.5)'
                }]
            },
            options: {
                responsive: true,
                plugins: { legend: { display: false } },
                scales: {
                    y: {
                        ticks: {
                            callback: function(value) { return '₹' + value; }
                        }
                    }
                }
            }
        });
    }

    // Render the full monthly trend chart once on page load
    fetch("/api/monthly-trend").then(r => r.json()).then(trend => {
        const ctx = document.getElementById('monthlyTrendChart')?.getContext('2d');
        if (!ctx) return;
        if (window.monthlyTrendChartInstance && typeof window.monthlyTrendChartInstance.destroy === 'function') {
            window.monthlyTrendChartInstance.destroy();
        }
        window.monthlyTrendChartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: trend.map(m => m.month),
                datasets: [{
                    label: 'Monthly Spend (INR)',
                    data: trend.map(m => m.total),
                    borderColor: '#2c3e50',
                    backgroundColor: 'rgba(44,62,80,0.1)',
                    fill: true,
                    tension: 0.3
                }]
            },
            options: {
                responsive: true,
                plugins: {
                    legend: { display: true },
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                return `₹${context.parsed.y.toLocaleString('en-IN', {minimumFractionDigits: 2})}`;
                            }
                        }
                    }
                },
                scales: {
                    y: {
                        ticks: {
                            callback: function(value) { return '₹' + value; }
                        }
                    }
                }
            }
        });
    });

    // Top N expenses table for selected range (now top 10)
    function renderTopExpensesTable(expenses) {
        let html = '<h3>Top 10 Expenses</h3>';
        if (!expenses.length) {
            html += '<div>No expenses for selected category/range.</div>';
            const el = document.getElementById('topExpenses');
            if (el) el.innerHTML = html;
            return;
        }
        html += '<table><thead><tr><th>Date</th><th>Description</th><th>Cost</th><th>Category</th></tr></thead><tbody>';
        expenses.slice(0, 10).forEach(e => {
            html += `<tr><td>${e.date}</td><td>${e.description}</td><td>₹${e.cost.toLocaleString('en-IN', {minimumFractionDigits: 2})}</td><td>${getCategoryIconHtml(e.category)}${e.category}</td></tr>`;
        });
        html += '</tbody></table>';
        const el = document.getElementById('topExpenses');
        if (el) el.innerHTML = html;
    }

    // Patch the loadMonthRangeDetail function to use new charts/tables
    function loadMonthRangeDetail(start, end, category) {
        let url = `/api/top-expenses/${start}/${end}`;
        if (category) url += `?category=${encodeURIComponent(category)}`;
        fetch(url).then(r => r.json()).then(expenses => {
            renderTopExpensesTable(expenses);
        });
    }

    // Section 3: Prediction
    fetch("/api/predict").then(r => r.json()).then(data => {
        const ctx = document.getElementById('predictionChart').getContext('2d');
        new Chart(ctx, {
            type: 'line',
            data: {
                labels: data.prediction.map(p => p.month),
                datasets: [{
                    label: 'Predicted Spend',
                    data: data.prediction.map(p => p.predicted),
                    borderColor: 'rgba(75, 192, 192, 1)',
                    backgroundColor: 'rgba(75, 192, 192, 0.2)',
                    fill: true
                }]
            },
            options: {
                responsive: true
            }
        });
    });

    // Category icon mapping (Font Awesome or similar)
    const categoryIcons = {
        'Food & Drink': 'fa-utensils',
        'Shopping': 'fa-shopping-cart',
        'Home': 'fa-home',
        'Transportation': 'fa-car',
        'Utilities': 'fa-bolt',
        'Entertainment': 'fa-film',
        'Travel': 'fa-plane',
        'Personal': 'fa-user',
        'Groceries': 'fa-apple-alt',
        'Health': 'fa-heartbeat',
        'Other': 'fa-ellipsis-h',
        'Uncategorized': 'fa-question',
        '': 'fa-question',
        'General': 'fa-receipt',
        'Rent': 'fa-building',
        'Gifts': 'fa-gift',
        'Education': 'fa-graduation-cap',
        'Pets': 'fa-paw',
        'Car': 'fa-car',
        'Taxi': 'fa-taxi',
        'Parking': 'fa-parking',
        'Fuel': 'fa-gas-pump',
        'Medical': 'fa-briefcase-medical',
        'Sports': 'fa-futbol',
        'Books': 'fa-book',
        'Clothes': 'fa-tshirt',
        'Water': 'fa-tint',
        'Internet': 'fa-wifi',
        'Mobile': 'fa-mobile-alt',
        'Phone': 'fa-phone',
        'Electricity': 'fa-bolt',
        'Gas': 'fa-fire',
        'Alcohol': 'fa-wine-bottle',
        'Coffee': 'fa-coffee',
        'Snacks': 'fa-cookie-bite',
        'Laundry': 'fa-soap',
        'Cleaning': 'fa-broom',
        'Childcare': 'fa-baby',
        'Charity': 'fa-hands-helping',
        'Insurance': 'fa-shield-alt',
        'Repairs': 'fa-tools',
        'Subscriptions': 'fa-credit-card',
        'Fees': 'fa-money-check-alt',
        'ATM': 'fa-university',
        'Bank': 'fa-university',
        'Salary': 'fa-money-bill-wave',
        'Investment': 'fa-chart-line',
        'Withdrawal': 'fa-money-bill-wave',
        'Deposit': 'fa-piggy-bank',
        'Loan': 'fa-hand-holding-usd',
        'Interest': 'fa-percentage',
        'Fine': 'fa-exclamation-triangle',
        'Toll': 'fa-road',
        'Petrol': 'fa-gas-pump',
        'Veggies': 'fa-carrot',
        'Amazon': 'fa-amazon',
        'Blinkit': 'fa-shopping-basket',
        'Parking': 'fa-parking',
        'Clothes': 'fa-tshirt',
        'Water': 'fa-tint',
        'Forum Mall food': 'fa-utensils',
        'Kota Kachori': 'fa-utensils',
        'Anand Sweets': 'fa-cookie-bite',
        'Native M2 Aquaguard': 'fa-tint',
        'Westside Clothes': 'fa-tshirt',
        'Amritsari Truly North Indi...': 'fa-utensils',
        'Petrol': 'fa-gas-pump',
        'Parking': 'fa-parking',
        'Veggies': 'fa-carrot',
        'Food': 'fa-utensils',
        'Snacks': 'fa-cookie-bite',
        'Groceries': 'fa-apple-alt',
        'Shopping': 'fa-shopping-cart',
        'Travel': 'fa-plane',
        'Health': 'fa-heartbeat',
        'Utilities': 'fa-bolt',
        'Entertainment': 'fa-film',
        'General': 'fa-receipt',
        'Miscellaneous': 'fa-ellipsis-h',
    };

    // Section: Category-wise Cumulative Spends
    function updateCategoryCumulativeChart(start, end, category) {
        let url = `/api/category-totals?start=${start}&end=${end}`;
        if (category) url += `&category=${encodeURIComponent(category)}`;
        fetch(url).then(r => r.json()).then(data => {
            if (category) {
                data = data.filter(c => c.category === category);
            }
            const ctx = document.getElementById('categoryCumulativeChart')?.getContext('2d');
            if (!ctx) return;
            if (window.categoryCumulativeChartInstance && typeof window.categoryCumulativeChartInstance.destroy === 'function') {
                window.categoryCumulativeChartInstance.destroy();
            }
            if (!data.length) {
                ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
                ctx.font = '16px Arial';
                ctx.fillText('No data for selected category/range', 20, 40);
                return;
            }
            window.categoryCumulativeChartInstance = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: data.map(c => c.category),
                    datasets: [{
                        label: 'Total Spend (INR)',
                        data: data.map(c => c.total),
                        backgroundColor: data.map((_, i) => `hsl(${i * 37 % 360}, 70%, 60%)`)
                    }]
                },
                options: {
                    indexAxis: 'y',
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                label: function(context) {
                                    return `₹${context.parsed.x.toLocaleString('en-IN', {minimumFractionDigits: 2})}`;
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            ticks: {
                                callback: function(value) { return '₹' + value; }
                            }
                        }
                    }
                }
            });
        });
    }

    // Add Font Awesome for icons
    if (!document.getElementById('fa-cdn')) {
        const fa = document.createElement('link');
        fa.rel = 'stylesheet';
        fa.href = 'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.15.4/css/all.min.css';
        fa.id = 'fa-cdn';
        document.head.appendChild(fa);
    }

    // Patch: Add icons to month-wise expenses table
    function getCategoryIconHtml(category) {
        const icon = categoryIcons[category] || '';
        if (icon) {
            return `<i class="fas ${icon}" style="margin-right:6px;"></i>`;
        } else {
            // fallback emoji for unknown category
            return '🗂️ ';
        }
    }
});